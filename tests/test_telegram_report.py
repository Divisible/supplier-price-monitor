"""Targeted tests for Telegram report formatting and empty-day handling."""
import unittest
from datetime import date

import pandas as pd

from telegram_report import build_report


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

    def test_no_comparable_days(self):
        history = pd.DataFrame([
            {"supplier": "ATB", "sku": "1", "product": "Лимон", "date": pd.Timestamp("2026-11-13"), "price": 80.0, "checked_at": pd.Timestamp("2026-11-13T08:00:00Z")},
        ])
        self.assertIn("Нет сопоставимых цен", build_report(history, date(2026, 11, 14)))


if __name__ == "__main__":
    unittest.main()
