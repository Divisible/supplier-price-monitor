"""Send a daily SPM price-change summary to a private Telegram chat.

Run only after the price collection has persisted its CSV files.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pandas as pd

from app import daily_changes, load_history

ROOT = Path(__file__).resolve().parent
KYIV = ZoneInfo("Europe/Kyiv")
DASHBOARD = "https://supplier-price-monitor-dzfqdggyymrq2sirg6aucu.streamlit.app/"


def build_report(history, today, status_counts=None):
    changes, comparable = daily_changes(history, today)
    rising = changes.loc[changes["change"].gt(0)]
    falling = changes.loc[changes["change"].lt(0)]
    lines = [
        f"SPM — изменения за {today:%d.%m.%Y} (Киев)",
        f"Подорожали: {len(rising)} | Подешевели: {len(falling)}",
    ]
    if not comparable:
        lines.append("Нет сопоставимых цен за сегодня и вчера.")
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


def send_message(token, chat_id, message):
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urlencode({"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}).encode()
    request = Request(endpoint, data=payload, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        # Never include the request URL (which contains the token) in logs.
        raise RuntimeError("Telegram API request failed; check token, chat ID and connectivity.") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message; check bot permissions and chat ID.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print("Telegram notification skipped: credentials not configured.")
        return
    history = load_history(ROOT / "data" / "price_history.csv")
    if history.empty:
        print("Telegram notification skipped: no validated history.")
        return
    today = datetime.now(KYIV).date()
    counts = read_status_counts(ROOT / "data" / "collection_status.csv")
    message = build_report(history, today, counts)
    send_message(token, chat_id, message)
    print("Telegram daily summary sent.")


if __name__ == "__main__":
    main()
