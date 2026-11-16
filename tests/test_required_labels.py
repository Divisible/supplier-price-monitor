"""Required filter semantics remain intact when their visual marker is hidden."""
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import spm_ui


class RequiredLabelTests(unittest.TestCase):
    def test_css_targets_only_required_marker(self):
        with patch.object(spm_ui.st, "html") as html:
            spm_ui.dashboard_layout()
        css = html.call_args.args[0]
        self.assertIn('[data-testid="stAppViewContainer"] [data-testid="stWidgetLabelRequired"] {\n        display:none;', css)
        self.assertNotIn('[data-testid="stWidgetLabel"] {', css)

    def test_promo_filters_keep_labels_defaults_required_and_behavior(self):
        at = AppTest.from_string('''
import pandas as pd
from spm_ui import dashboard_layout
from app import promo_filters
dashboard_layout()
rows = pd.DataFrame([dict(supplier="Фора", sku="1", product="Лимон"), dict(supplier="АТБ", sku="2", product="Банан")])
mapping = rows[["supplier", "sku"]].assign(product_id=["FRU001", "FRU003"], canonical_name=["Лимон, кг", "Банан, кг"])
filtered, _ = promo_filters(rows, mapping, "promo_active")
import streamlit as st
st.dataframe(filtered)
''').run()
        self.assertFalse(at.exception)
        self.assertEqual([s.label for s in at.selectbox], ["Поставщик", "Главный товар"])
        self.assertTrue(all(s.proto.required for s in at.selectbox))
        self.assertEqual(len(at.dataframe[0].value), 2)
        at.selectbox[0].select("Фора").run()
        self.assertFalse(at.exception)
        self.assertEqual(at.dataframe[0].value.supplier.tolist(), ["Фора"])


if __name__ == "__main__":
    unittest.main()
