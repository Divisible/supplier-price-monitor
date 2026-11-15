"""Focused search and price-direction presentation regressions."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from PIL import Image
from streamlit.testing.v1 import AppTest

import spm_ui
from price_notes import find_font, render_notes
from telegram_report import build_report


class PriceDirectionTests(unittest.TestCase):
    def test_table_dynamics_and_discounts_have_separate_colors(self):
        data = pd.DataFrame(dict(change=[10, -10, 0], change_pct=[20, -20, 0],
                                 promo_difference=[-10, 0, None], discount_pct=[20, 0, None]))
        with patch.object(spm_ui.st, "dataframe") as show:
            spm_ui.styled_table(data, {})
        style = show.call_args.args[0]
        colors = style._compute().ctx
        self.assertIn(("color", "#15803D"), colors[0, 0])
        self.assertIn(("color", "#B91C1C"), colors[1, 0])
        self.assertIn(("color", "#64748B"), colors[2, 0])
        self.assertIn(("color", "#15803D"), colors[0, 2])
        self.assertIn(("color", "#15803D"), colors[0, 3])
        pd.testing.assert_frame_equal(style.data, data)

    def test_png_arrows_and_text_keep_signs(self):
        changes = pd.DataFrame([
            dict(product="Лимон, кг", supplier="Фора", previous_price=100, price=110, change=10, change_pct=10),
            dict(product="Банан, кг", supplier="АТБ", previous_price=100, price=90, change=-10, change_pct=-10),
        ])
        with tempfile.TemporaryDirectory() as folder:
            paths = render_notes(changes, 2, date(2026, 11, 16), Path(folder), "Проверка", find_font())
            with Image.open(paths[0]) as image:
                self.assertEqual(image.size, (1600, 900))
                self.assertEqual(image.getpixel((73, 234)), (21, 128, 61))
                self.assertEqual(image.getpixel((841, 240)), (185, 28, 28))
        text = build_report(pd.DataFrame(), date(2026, 11, 16), comparison_data=(changes, 2, []))
        self.assertIn("🟢 ↑ Лимон", text)
        self.assertIn("🔴 ↓ Банан", text)
        self.assertIn("(+10.00%)", text)
        self.assertIn("(-10.00%)", text)

    def test_overview_search_clear_sort_and_pagination(self):
        script = '''
import pandas as pd
from app import show_current_prices
catalog = pd.DataFrame([dict(supplier="Фора", sku=str(i), product=f"Товар {i}", url=f"https://fora.ua/product/{i}") for i in range(31)])
mapping = catalog[["supplier", "sku"]].assign(canonical_name=["Лимон" if i < 16 else "Банан" for i in range(31)])
fresh = catalog[["supplier", "sku"]].assign(price=list(range(31,0,-1)), is_promo=False, regular_price=float("nan"), reported_discount=0)
show_current_prices(catalog, fresh, mapping)
'''
        at = AppTest.from_string(script).run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.dataframe[0].value), 15)
        at.button(key="overview_page_2").click().run()
        self.assertEqual(at.session_state["overview_page"], 2)
        at.text_input(key="overview_search").set_value("лИм").run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["overview_page"], 1)
        self.assertEqual(set(at.dataframe[0].value.canonical_name), {"Лимон"})
        self.assertFalse(any(str(b.key).startswith("overview_suggestion_") for b in at.button))
        at.selectbox[0].select("normal_price").run()
        self.assertTrue(at.dataframe[0].value.normal_price.is_monotonic_increasing)
        next(b for b in at.button if b.label == "×").click().run()
        self.assertEqual(at.session_state["overview_search"], "")
        self.assertEqual(at.session_state["overview_page"], 1)
        self.assertEqual(len(at.dataframe[0].value), 15)
        self.assertEqual(at.selectbox[0].value, "normal_price")
        self.assertFalse(at.exception)


if __name__ == "__main__":
    unittest.main()
