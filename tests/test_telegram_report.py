"""Targeted tests for Telegram report formatting and empty-day handling."""
import unittest
import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import date
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pandas as pd

from telegram_report import build_report
import telegram_report as report


class TelegramReportTests(unittest.TestCase):
    def test_changed_and_unchanged_prices(self):
        history = pd.DataFrame([
            {"supplier": "ATB", "sku": "1", "product": "Лимон", "date": pd.Timestamp("2026-11-13"), "price": 80.0, "checked_at": pd.Timestamp("2026-11-13T08:00:00Z")},
            {"supplier": "ATB", "sku": "1", "product": "Лимон", "date": pd.Timestamp("2026-11-14"), "price": 81.0, "checked_at": pd.Timestamp("2026-11-14T08:00:00Z")},
            {"supplier": "ATB", "sku": "2", "product": "Груша", "date": pd.Timestamp("2026-11-13"), "price": 50.0, "checked_at": pd.Timestamp("2026-11-13T08:00:00Z")},
            {"supplier": "ATB", "sku": "2", "product": "Груша", "date": pd.Timestamp("2026-11-14"), "price": 50.0, "checked_at": pd.Timestamp("2026-11-14T08:00:00Z")},
        ])
        report = build_report(history, date(2026, 11, 14))
        self.assertIn("Подорожали: 1", report)
        self.assertIn("80.00 → 81.00", report)
        self.assertNotIn("Груша", report)

    def test_status_counts_are_labeled_as_current(self):
        history = pd.DataFrame([
            {"supplier": "ATB", "sku": "1", "product": "Лимон", "date": pd.Timestamp("2026-11-13"), "price": 80.0, "checked_at": pd.Timestamp("2026-11-13T08:00:00Z")},
        ])
        report = build_report(history, date(2026, 11, 14), {"error": 2, "out_of_stock": 1})
        self.assertIn("Ошибки: 2", report)
        self.assertIn("Нет в наличии: 1", report)
        self.assertIn("текущие статусы", report)

    def test_no_comparable_days(self):
        history = pd.DataFrame([
            {"supplier": "ATB", "sku": "1", "product": "Лимон", "date": pd.Timestamp("2026-11-13"), "price": 80.0, "checked_at": pd.Timestamp("2026-11-13T08:00:00Z")},
        ])
        self.assertIn("Нет сопоставимых цен", build_report(history, date(2026, 11, 14)))


class ImageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.calls = []
        self.network = patch.object(report, "urlopen", side_effect=self.respond)
        self.network.start()
        self.addCleanup(self.network.stop)

    def respond(self, request, **kwargs):
        self.calls.append(request)
        return io.BytesIO(b'{"ok": true, "result": []}')

    def pages(self, count):
        paths = [self.folder / f"page-{n}.png" for n in range(count)]
        for n, path in enumerate(paths):
            path.write_bytes(f"image-{n}".encode())
        return paths

    def parts(self, request):
        headers = f"Content-Type: {request.get_header('Content-type')}\r\n\r\n".encode()
        return list(BytesParser(policy=policy.default).parsebytes(headers + request.data).iter_parts())

    def test_single_photo_and_ordered_albums(self):
        for count in (1, 2, 10, 11, 21):
            with self.subTest(count=count):
                self.calls.clear()
                report.send_images("fake-token", "123", self.pages(count), "Дата · +2 / −1 · Dashboard")
                uploaded = []
                for request in self.calls:
                    parts = self.parts(request)
                    uploaded.extend(p.get_payload(decode=True) for p in parts if p.get_filename())
                    media = [p for p in parts if p.get_param("name", header="content-disposition") == "media"]
                    if media:
                        items = json.loads(media[0].get_payload(decode=True))
                        self.assertTrue(2 <= len(items) <= 10)
                        self.assertIn("caption", items[0])
                        filenames = [p.get_param("name", header="content-disposition") for p in parts if p.get_filename()]
                        self.assertEqual([i["media"] for i in items], [f"attach://{n}" for n in filenames])
                        self.assertTrue(request.full_url.endswith("/sendMediaGroup"))
                    else:
                        self.assertTrue(request.full_url.endswith("/sendPhoto"))
                self.assertEqual(uploaded, [f"image-{n}".encode() for n in range(count)])

    def history(self, count):
        return pd.DataFrame([
            dict(supplier="Фора", sku=str(n), product=f"Товар {n}, кг", price=price,
                 date=pd.Timestamp(day), checked_at=pd.Timestamp(day, tz="Europe/Kyiv"))
            for n in range(count)
            for day, price in [("2026-11-13", 100.), ("2026-11-14", 110. if n % 4 else 90.)]
        ])

    def test_real_generation_20_and_40_changes(self):
        for count in (20, 40):
            self.calls.clear()
            paths = report.generate_images(self.history(count), date(2026, 11, 14), self.folder)
            self.assertEqual(len(paths), 3 if count == 20 else 5)
            report.send_images("fake-token", "123", paths, "Report")
            files = [p for p in self.parts(self.calls[0]) if p.get_filename()]
            self.assertEqual([p.get_payload(decode=True) for p in files], [p.read_bytes() for p in paths])

    def run_main(self, generation, response=None):
        with patch.dict(report.os.environ, {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "123"}), \
                patch.object(report, "load_history", return_value=self.history(1)), \
                patch.object(report, "generate_images", side_effect=generation), \
                patch.object(report, "urlopen", side_effect=response or self.respond):
            report.main()

    def test_success_sends_no_text(self):
        self.run_main(lambda *args: self.pages(2))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].full_url.endswith("/sendMediaGroup"))

    def test_generation_failure_falls_back(self):
        self.run_main(RuntimeError("font unavailable"))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0].full_url.endswith("/sendMessage"))

    def test_rejected_png_uses_text_fallback(self):
        def response(request, **kwargs):
            self.calls.append(request)
            body = b'{"ok":false,"description":"fake-token"}' if len(self.calls) == 1 else b'{"ok":true}'
            return io.BytesIO(body)
        output = io.StringIO()
        with redirect_stdout(output):
            self.run_main(lambda *args: self.pages(1), response)
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in self.calls], ["sendPhoto", "sendMessage"])
        self.assertNotIn("fake-token", output.getvalue())

    def test_long_fallback_preserves_all_text(self):
        from urllib.parse import parse_qs
        message = "Цена 🍋\n" * 900
        report.send_message("fake-token", "123", message)
        chunks = [parse_qs(r.data.decode())["text"][0] for r in self.calls]
        self.assertEqual("".join(chunks), message)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))

    def test_partial_failure_never_reuploads_pages(self):
        def response(request, **kwargs):
            self.calls.append(request)
            if len(self.calls) == 2:
                raise TimeoutError("https://api.telegram.org/botfake-token/sendPhoto")
            return io.BytesIO(b'{"ok":true}')
        output = io.StringIO()
        with redirect_stdout(output):
            self.run_main(lambda *args: self.pages(21), response)
        self.assertEqual([r.full_url.rsplit("/", 1)[1] for r in self.calls],
                         ["sendMediaGroup", "sendMediaGroup", "sendMessage"])
        self.assertNotIn("fake-token", output.getvalue())

    def test_api_errors_are_safe(self):
        url = "https://api.telegram.org/botfake-token/sendPhoto"
        failures = [HTTPError(url, 429, "fake-token", {}, None), URLError(url),
                    TimeoutError(url), io.BytesIO(b'not json fake-token'),
                    io.BytesIO(b'{"ok":false,"description":"fake-token"}')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                effect = failure if isinstance(failure, Exception) else lambda *args, **kwargs: failure
                with patch.object(report, "urlopen", side_effect=effect):
                    with self.assertRaises(RuntimeError) as error:
                        report.send_images("fake-token", "123", self.pages(1), "report")
                self.assertNotIn("fake-token", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_missing_credentials_skip_everything(self):
        with patch.dict(report.os.environ, {}, clear=True), patch.object(report, "load_history") as load:
            report.main()
        load.assert_not_called()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
