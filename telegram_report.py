"""Send a daily SPM price-change summary to a private Telegram chat.

Run only after the price collection has persisted its CSV files.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from uuid import uuid4

import pandas as pd

from app import daily_changes, load_history

ROOT = Path(__file__).resolve().parent
KYIV = ZoneInfo("Europe/Kyiv")
DASHBOARD = "https://supplier-price-monitor-dzfqdggyymrq2sirg6aucu.streamlit.app/"


def build_report(history, today, status_counts=None, comparison_data=None, mode="morning"):
    changes, comparable, promos = comparison_data if comparison_data is not None else (*daily_changes(history, today), [])
    rising = changes.loc[changes["change"].gt(0)]
    falling = changes.loc[changes["change"].lt(0)]
    lines = [
        f"SPM — {'утро: к предыдущему дню' if mode == 'morning' else 'вечер: к утреннему сбору'} · {today:%d.%m.%Y} (Киев)",
        f"Подорожали: {len(rising)} | Подешевели: {len(falling)}",
    ]
    if not comparable:
        lines.append("Нет сопоставимых цен за сегодня и вчера." if mode == "morning" else "Нет сопоставимых успешных утренних и вечерних наблюдений.")
    elif changes.empty:
        lines.append("Сопоставимые цены не изменились.")
    else:
        # Keep Telegram messages short; full details remain in the Dashboard.
        for row in changes.sort_values("change_pct", key=lambda s: s.abs(), ascending=False).head(20).itertuples():
            arrow = "↑" if row.change > 0 else "↓"
            lines.append(
                f"{arrow} {row.product} · {row.supplier}: "
                f"{row.previous_price:.2f} → {row.price:.2f} грн "
                f"({row.change_pct:+.2f}%)"
            )
        if len(changes) > 20:
            lines.append(f"Ещё изменений: {len(changes) - 20}.")
    lines.extend(promos)
    if status_counts is not None:
        lines.append("Состояние последнего сбора:")
        lines.append(f"Ошибки: {status_counts.get('error', 0)} | Нет в наличии: {status_counts.get('out_of_stock', 0)} | Без цены: {status_counts.get('price_missing', 0)}")
        lines.append("Это текущие статусы, не новые события за сегодня.")
    lines.append(f"Подробнее: {DASHBOARD}")
    return "\n".join(lines)


def read_status_counts(path):
    """Count latest collection statuses; do not infer historical transitions."""
    import csv
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    counts = {}
    for row in rows:
        value = row.get("status", "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def telegram_request(token, method, payload, content_type):
    try:
        request = Request(f"https://api.telegram.org/bot{token}/{method}", data=payload,
                          headers={"Content-Type": content_type}, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Rejected response")
    except Exception:
        # Never include the request URL (which contains the token) in logs.
        raise RuntimeError("Telegram API request failed; delivery may be unconfirmed.") from None


def send_message(token, chat_id, message):
    # At most 4000 UTF-16 units, even when every character is an emoji.
    for start in range(0, len(message), 2000):
        payload = urlencode({"chat_id": chat_id, "text": message[start:start + 2000],
                             "disable_web_page_preview": "true"}).encode()
        telegram_request(token, "sendMessage", payload, "application/x-www-form-urlencoded")


def send_images(token, chat_id, paths, caption):
    if not paths:
        raise ValueError("No report images generated")
    # No retries: after a timeout Telegram may have accepted the upload.
    # A later batch failure never restarts previously sent batches.
    for start in range(0, len(paths), 10):
        batch = paths[start:start + 10]
        fields = {"chat_id": chat_id}
        files = {}
        if len(batch) == 1:
            method = "sendPhoto"
            files["photo"] = batch[0]
            fields["caption"] = caption
        else:
            method = "sendMediaGroup"
            media = []
            for index, path in enumerate(batch):
                name = f"page{start + index + 1}"
                files[name] = path
                item = {"type": "photo", "media": f"attach://{name}"}
                if index == 0:
                    item["caption"] = caption
                media.append(item)
            fields["media"] = json.dumps(media, ensure_ascii=False)
        boundary = uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for name, path in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.png"\r\nContent-Type: image/png\r\n\r\n'.encode())
            parts.extend([path.read_bytes(), b"\r\n"])
        parts.append(f"--{boundary}--\r\n".encode())
        telegram_request(token, method, b"".join(parts), f"multipart/form-data; boundary={boundary}")


def generate_images(history, today, output, comparison_data=None, mode="morning"):
    # Keep rendering imports inside the fallback boundary, including Pillow.
    from price_notes import collection_summary, find_font, render_notes
    changes, comparable = comparison_data[:2] if comparison_data is not None else daily_changes(history, today)
    footer = collection_summary(ROOT / "data" / "collection_status.csv")
    return render_notes(changes, comparable, today, output, footer, find_font(), mode=mode)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram notification skipped: credentials not configured.")
        return
    history_path = ROOT / "data" / "price_history.csv"
    history = load_history(history_path) if history_path.exists() else pd.DataFrame()
    snapshot_path = os.environ.get("SPM_CURRENT_SNAPSHOT")
    if history.empty and not snapshot_path:
        print("Telegram notification skipped: no validated history.")
        return
    today = datetime.now(KYIV).date()
    counts = read_status_counts(ROOT / "data" / "collection_status.csv")
    comparison_data, mode = None, "morning"
    if snapshot_path:
        from collection_run import comparison
        current = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        if current["date"] != today.isoformat():
            print("Telegram notification skipped: snapshot is not from today.")
            return
        mode = current["mode"]
        baseline = Path(os.environ.get("SPM_MORNING_SNAPSHOT", "missing-snapshot.json"))
        try:
            morning = json.loads(baseline.read_text(encoding="utf-8")) if baseline.is_file() else None
        except (OSError, ValueError):
            morning = None
            print("Morning snapshot unreadable; intraday comparison unavailable.")
        comparison_data = comparison(history, current, morning)
        counts = {}
        for row in current["statuses"]:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    message = build_report(history, today, counts, comparison_data, mode)
    caption = "\n".join([*message.splitlines()[:2], DASHBOARD])
    if comparison_data is not None:
        # Caption is compact; full promotion details remain in the text fallback.
        extras = [*comparison_data[2], f"Сбор: ошибок {counts.get('error', 0)}, нет в наличии {counts.get('out_of_stock', 0)}, без цены {counts.get('price_missing', 0)}"]
        caption = "\n".join([*message.splitlines()[:2], extras.pop(), DASHBOARD])
        for index, line in enumerate(extras):
            if len((caption + "\n" + line).encode("utf-16-le")) // 2 > 900:
                caption += f"\nЕщё строк о промо/сравнении: {len(extras) - index}. Подробнее в Dashboard."
                break
            caption += "\n" + line
    try:
        with TemporaryDirectory(prefix="spm-price-notes-") as output:
            paths = (generate_images(history, today, Path(output), comparison_data, mode)
                     if comparison_data is not None else generate_images(history, today, Path(output)))
            send_images(token, chat_id, paths, caption)
    except Exception:
        print("PNG report failed or delivery unconfirmed; sending text fallback without retrying images.")
        send_message(token, chat_id, message)
        print("Telegram text fallback sent.")
        return
    print("Telegram PNG report sent.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Telegram notification failed; check source data, credentials and connectivity.") from None
