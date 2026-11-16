"""Focused diagnostics summary checks; all CSV fixtures are temporary."""
import csv
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from app import diagnostic_summary


class DiagnosticSummaryTests(unittest.TestCase):
    def test_statuses_denominators_and_supplier_times(self):
        positions = pd.DataFrame({
            "supplier": ["A"] * 7 + ["B"],
            "status": ["ok", "error", "price_missing", "out_of_stock", "future_status", "", None, "ok"],
            "checked_at": ["2026-11-16T07:30:00Z"] * 6 + [None, "2026-11-16T06:00:00Z"],
        })
        summary = diagnostic_summary(positions).set_index("supplier")
        self.assertEqual(summary.loc["A", "success"], "1/7")
        self.assertEqual(summary.loc["A", "errors"], 4)
        self.assertEqual(summary.loc["A", "unavailable"], 1)
        self.assertEqual(summary.loc["Всего", "success"], "2/8")
        self.assertEqual(summary.loc["Всего", "errors"], 4)
        self.assertEqual(summary.loc["A", "checked_at"].hour, 9)
        self.assertEqual(summary.loc["B", "checked_at"].hour, 8)
        self.assertEqual(summary.loc["Всего", "checked_at"], summary.loc["A", "checked_at"])

    def test_empty_catalog_and_no_attempts(self):
        empty = pd.DataFrame(columns=["supplier", "status", "checked_at"])
        total = diagnostic_summary(empty).iloc[0]
        self.assertEqual(total["supplier"], "Всего")
        self.assertEqual(total["success"], "0/0")
        self.assertTrue(pd.isna(total["checked_at"]))
        missing = diagnostic_summary(pd.DataFrame(dict(supplier=["A", "A"], status=[None, None], checked_at=[None, None])))
        self.assertEqual(missing.iloc[0]["success"], "0/2")
        self.assertEqual(missing.iloc[0]["errors"], 0)
        self.assertTrue(missing.checked_at.isna().all())

    def run_ui(self, root):
        script = f'''
from datetime import date
from pathlib import Path
import pandas as pd
import app
app.HISTORY = Path({str(root / "price_history.csv")!r})
catalog = pd.DataFrame([dict(supplier="A", sku=str(i), product=f"Товар {{i}}", url=f"https://example.com/{{i}}") for i in range(17)] + [dict(supplier="B", sku="1", product="Товар B", url="https://example.com/b")])
current = pd.DataFrame([dict(supplier="A", sku="0", price=100, date=pd.Timestamp("2026-11-10"))])
history = pd.DataFrame([dict(checked_at=pd.Timestamp("2026-11-10T12:00:00Z"))])
app.show_diagnostics(current, date(2026,11,16), catalog, None, history)
'''
        return AppTest.from_string(script).run()

    def test_summary_ui_filters_pagination_and_history_independence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "collection_status.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["supplier", "sku", "status", "checked_at", "reason"])
                writer.writeheader()
                writer.writerow(dict(supplier="A", sku="0", status="price_missing", checked_at="2026-11-16T07:30:00Z", reason="no price"))
                writer.writerow(dict(supplier="B", sku="1", status="ok", checked_at="2026-11-16T06:00:00Z", reason=""))
                writer.writerow(dict(supplier="removed", sku="99", status="ok", checked_at="2026-11-16T20:00:00Z", reason=""))
            before = path.read_bytes()
            at = self.run_ui(root)
            self.assertFalse(at.exception)
            self.assertEqual(len(at.metric), 0)
            summary = at.dataframe[0].value
            self.assertEqual(list(summary.columns), ["supplier", "success", "errors", "unavailable", "checked_at"])
            self.assertEqual(summary.supplier.tolist(), ["A", "B", "Всего"])
            self.assertEqual(summary.success.tolist(), ["0/17", "1/1", "1/18"])
            self.assertIn("16.11.2026 09:30", at.caption[0].value)
            self.assertIn("Записей истории: 1", at.caption[0].value)
            self.assertIn("Без попытки — 16", at.caption[1].value)
            self.assertTrue(any("Устаревшие цены: 1" in w.value for w in at.warning))
            self.assertEqual(len(at.dataframe[1].value), 15)
            at.button(key="diag_page_2").click().run()
            self.assertEqual(len(at.dataframe[1].value), 3)
            at.selectbox(key="diag_supplier").select("B").run()
            self.assertEqual(len(at.dataframe[1].value), 1)
            self.assertEqual(at.session_state["diag_page"], 1)
            pd.testing.assert_frame_equal(at.dataframe[0].value, summary)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_and_header_only_diagnostics_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for header_only in (False, True):
                if header_only:
                    (root / "collection_status.csv").write_text("supplier,sku,status,checked_at,reason\n", encoding="utf-8")
                at = self.run_ui(root)
                self.assertFalse(at.exception)
                self.assertEqual(at.dataframe[0].value.iloc[-1]["success"], "0/18")
                self.assertTrue(at.dataframe[0].value.checked_at.isna().all())
                self.assertIn("Последний сбор по Киеву: Нет данных", at.caption[0].value)
                self.assertIn("Без попытки — 18", at.caption[1].value)


if __name__ == "__main__":
    unittest.main()
