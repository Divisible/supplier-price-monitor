"""Targeted twice-daily reporting checks. No site or Telegram requests."""
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import collection_run as run
import telegram_report as telegram
from app import load_history


def stamp(hour, day="2026-11-15"):
    return datetime.fromisoformat(f"{day}T{hour}:00:00+02:00")


def row(sku="1", price="100", promo="false", hour="10", day="2026-11-15"):
    return dict(date=day, checked_at=stamp(hour, day).isoformat(), supplier="Фора", sku=sku,
                product="Лимон, кг", currency="UAH", price=price, url=f"https://fora.ua/product/lymon-{sku}",
                regular_price="125" if promo == "true" else "", discount_pct="20" if promo == "true" else "0",
                is_promo=promo)


def snapshot(rows, mode="morning"):
    hour = "09" if mode == "morning" else "17"
    return dict(date="2026-11-15", ref="refs/heads/main", mode=mode, rows=rows, statuses=[],
                started_at=stamp(hour).isoformat(), finished_at=(stamp(hour) + timedelta(minutes=20)).isoformat())


class RunTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "data").mkdir()
        self.work = self.root / "run"

    def write(self, name, rows):
        with (self.root / "data" / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_timezone_dst_and_delayed_slots(self):
        for day, offset in (("2026-01-15", "+02:00"), ("2026-07-15", "+03:00"),
                            ("2026-03-29", "+03:00"), ("2026-10-25", "+02:00")):
            for cron, hour, mode in (("30 9 * * *", "09:30", "morning"), ("0 17 * * *", "17:00", "evening")):
                now = datetime.fromisoformat(f"{day}T{hour}:00{offset}")
                self.assertEqual(run.run_slot(cron, now, now + timedelta(minutes=45)), (mode, True))
        self.assertEqual(run.run_slot("30 9 * * *", stamp("10"), stamp("17")), ("morning", False))
        self.assertFalse(run.run_slot("0 17 * * *", stamp("17"), stamp("17", "2026-11-16"))[1])
        self.assertEqual(run.run_slot("", stamp("18"), stamp("18")), ("evening", True))

    def test_backup_successful_snapshot_and_unchanged_timestamp(self):
        rows = [row(day="2026-11-14"), row(), row("2")]
        self.write("price_history.csv", rows)
        before = (self.root / "data/price_history.csv").read_bytes()
        run.prepare(self.root, self.work, "0 17 * * *", stamp("17"), stamp("17"))
        statuses = [dict(supplier="Фора", sku=sku, checked_at=stamp("18").isoformat(), status=status, reason="")
                    for sku, status in (("1", "ok"), ("2", "error"))]
        self.write("collection_status.csv", statuses)
        current = run.capture(self.root, self.work, stamp("19"))
        self.assertEqual([r["sku"] for r in current["rows"]], ["1"])
        self.assertEqual(current["rows"][0]["checked_at"], rows[1]["checked_at"])
        self.assertEqual((self.work / "backup/price_history.csv").read_bytes(), before)
        self.assertEqual((self.root / "data/price_history.csv").read_bytes(), before)
        statuses[0]["checked_at"] = stamp("10").isoformat()
        self.write("collection_status.csv", statuses)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            run.capture(self.root, self.work, stamp("19"))

    def test_reject_past_history_mutation(self):
        self.write("price_history.csv", [row(day="2026-11-14")])
        run.prepare(self.root, self.work, "0 17 * * *", stamp("17"), stamp("17"))
        self.write("price_history.csv", [row(day="2026-11-14", price="101")])
        with self.assertRaisesRegex(ValueError, "outside the current day"):
            run.capture(self.root, self.work, stamp("18"))

    def test_evening_rise_fall_equal_and_promotion_only(self):
        morning = snapshot([row(str(i)) for i in range(1, 5)])
        evening = snapshot([row("1", "110"), row("2", "90"), row("3"), row("4", promo="true")], "evening")
        changes, count, promos = run.comparison(pd.DataFrame(), evening, morning)
        self.assertEqual(count, 4)
        self.assertEqual(dict(zip(changes.sku, changes.change_pct)), {"1": 10, "2": -10})
        self.assertIn("Акция началась", "\n".join(promos))
        evening["rows"][3]["is_promo"] = ""
        self.assertNotIn("Акция завершилась", "\n".join(run.comparison(pd.DataFrame(), evening, morning)[2]))
        text = telegram.build_report(pd.DataFrame(), stamp("18").date(), {}, (changes, count, promos), "evening")
        self.assertIn("к утреннему сбору", text)
        self.assertIn("100.00 → 110.00", text)

    def test_missing_failed_or_wrong_day_morning_never_uses_old_prices(self):
        evening = snapshot([row(price="150")], "evening")
        for morning in (None, snapshot([]), dict(snapshot([row()]), date="2026-11-14"),
                        dict(snapshot([row()]), ref="refs/heads/other")):
            changes, count, _ = run.comparison(pd.DataFrame([row(price="99", day="2026-11-14")]), evening, morning)
            self.assertEqual(count, 0)
            self.assertTrue(changes.empty)
        changes, count, _ = run.comparison(pd.DataFrame(), snapshot([], "evening"), snapshot([row()]))
        self.assertEqual(count, 0)
        self.assertTrue(changes.empty)

    def test_promo_end_and_changed_terms(self):
        morning = snapshot([row(promo="true"), row("2", promo="true")])
        changed = row("2", promo="true")
        changed.update(price="90", discount_pct="28")
        evening = snapshot([row(), changed], "evening")
        changes, count, promos = run.comparison(pd.DataFrame(), evening, morning)
        self.assertEqual(count, 2)
        self.assertEqual(changes.sku.tolist(), ["2"])
        self.assertIn("Акция завершилась", "\n".join(promos))
        self.assertIn("Акция условия изменились", "\n".join(promos))

    def test_morning_excludes_failed_retained_prices(self):
        self.write("price_history.csv", [row(day="2026-11-14"), row("2", day="2026-11-14"),
                                         row(price="110"), row("2", price="150")])
        history = load_history(self.root / "data/price_history.csv")
        current = snapshot([row(price="110")])
        changes, count, promos = run.comparison(history, current)
        self.assertEqual(count, 1)
        self.assertEqual(changes.sku.tolist(), ["1"])
        text = telegram.build_report(history, stamp("10").date(), {"error": 1}, (changes, count, promos))
        self.assertIn("100.00 → 110.00", text)
        self.assertNotIn("150.00", text)

    def test_total_failure_sends_status_report_not_stale_prices(self):
        current = snapshot([], "evening")
        current["statuses"] = [dict(status="error")]
        path = self.root / "snapshot.json"
        path.write_text(json.dumps(current), encoding="utf-8")
        clock = type("Clock", (), {"now": staticmethod(lambda zone: stamp("18"))})
        with patch.dict(telegram.os.environ, {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "1",
                                             "SPM_CURRENT_SNAPSHOT": str(path), "SPM_MORNING_SNAPSHOT": str(self.root / "missing")}), \
                patch.object(telegram, "ROOT", self.root), patch.object(telegram, "datetime", clock), \
                patch.object(telegram, "generate_images", side_effect=ValueError("render failure")), \
                patch.object(telegram, "send_message") as send:
            telegram.main()
        self.assertIn("Ошибки: 1", send.call_args.args[2])
        self.assertIn("Подорожали: 0", send.call_args.args[2])
        self.assertIn("Утренний снимок отсутствует", send.call_args.args[2])

    def test_evening_png_for_changed_unchanged_and_missing_pairs(self):
        from PIL import Image
        morning = snapshot([row()])
        for price, expected in (("110", 1), ("100", 0), (None, 0)):
            current = snapshot([row(price=price)] if price else [], "evening")
            data = run.comparison(pd.DataFrame(), current, morning)
            self.assertEqual(len(data[0]), expected)
            paths = telegram.generate_images(pd.DataFrame(), stamp("18").date(), self.root / "png", data, "evening")
            self.assertEqual(len(paths), 1)
            with Image.open(paths[0]) as image:
                self.assertEqual(image.size, (1600, 900))


if __name__ == "__main__":
    unittest.main()
