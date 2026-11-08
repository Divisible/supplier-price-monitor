"""Targeted promotion/history regressions; collection uses recorded page text."""
import csv
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# Browser/network are mocked; production parsing and persistence run unchanged.
sys.modules.setdefault("playwright.sync_api", types.SimpleNamespace(sync_playwright=MagicMock()))
import price_monitor as monitor
from openpyxl import load_workbook

NORMAL = "Лимон\n32550\nкг\n79\n90 грн\nВ кошик\nВ наявності"
PROMO = "Банан\n32485\nкг\n76,90 грн\n-10%\n68\n90 грн\nВ кошик\nВ наявності"
BASELINE = "date,checked_at,supplier,sku,product,currency,price,url\r\n2026-11-05,2026-11-05T18:31:20.233073+02:00,Фора,32485,\"Банан, кг\",UAH,74.80,https://fora.ua/product/banan-32485\r\n2026-11-05,2026-11-05T18:31:20.233073+02:00,Фора,32546,\"Апельсин, кг\",UAH,89.90,https://fora.ua/product/apelsyn-32546\r\n2026-11-05,2026-11-05T18:31:20.233073+02:00,Фора,32550,\"Лимон, кг\",UAH,79.90,https://fora.ua/product/lymon-32550\r\n"

class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = patch.multiple(monitor, ROOT=root, HISTORY=root/"history.csv", OUTPUT=root/"report.xlsx")
        self.paths.start()
        self.addCleanup(self.paths.stop)
        monitor.HISTORY.write_text(BASELINE, encoding="utf-8", newline="")
        self.old = monitor.load_history()
        with (root/"products.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=monitor.LEGACY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.old)

    def collect(self, fail=False, day="2026-11-06"):
        page = MagicMock()
        current = {}
        products = {r["url"]: r for r in self.old}
        def navigate(url, **kwargs):
            current.clear()
            current.update(products[url])
            return types.SimpleNamespace(status=200)
        def details(script, sku):
            if sku == "32485":
                return PROMO + ("\n" if not fail else "")
            if fail and sku == "32550":
                return NORMAL.replace("79\n90 грн", "79\n90 грн\n80\n90 грн")
            return current["product"]+"\n"+sku+"\nкг\n"+current["price"].replace(".", "\n")+" грн\nВ кошик"
        page.goto.side_effect = navigate
        page.title.side_effect = lambda: current["product"]
        page.locator.return_value.first.evaluate.side_effect = details
        browser = MagicMock()
        browser.new_page.return_value = page
        playwright = MagicMock()
        playwright.return_value.__enter__.return_value.chromium.launch.return_value = browser
        clock = MagicMock()
        clock.now.return_value = datetime.fromisoformat(day + "T12:00:00+02:00")
        with patch.object(monitor, "sync_playwright", playwright), patch.object(monitor, "datetime", clock):
            monitor.main()

    def test_normal(self):
        self.assertEqual(monitor.parse_product_prices(NORMAL, "32550"),
                         dict(price="79.90", regular_price="", discount_pct="0", is_promo="false"))

    def test_promo(self):
        self.assertEqual(monitor.parse_product_prices(PROMO, "32485"),
                         dict(price="68.90", regular_price="76.90", discount_pct="10.40", is_promo="true"))

    def test_ambiguous(self):
        for text in (PROMO.replace("68\n90", "68\n90 грн\n67\n90"),
                     PROMO.replace("76,90", "60,90"),
                     PROMO.replace("-10%\n", ""),
                     NORMAL.replace("В кошик", "Рекомендації\nВ кошик"),
                     NORMAL + "\n32550",
                     NORMAL.replace("79\n90", "0\n00")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                monitor.parse_product_prices(text, "32485" if "32485" in text else "32550")

    def test_legacy_migration(self):
        original = list(csv.DictReader(BASELINE.splitlines()))
        self.collect(day="2026-11-05")
        migrated = monitor.load_history()
        self.assertEqual([{k: r[k] for k in monitor.LEGACY_FIELDS} for r in migrated], original)
        self.assertTrue(all(r["is_promo"] == "" for r in migrated))
        self.assertEqual(len(migrated), 3)

    def test_same_day_retry(self):
        self.collect()
        first = monitor.HISTORY.read_bytes()
        self.collect()
        self.assertEqual(monitor.HISTORY.read_bytes(), first)
        self.assertEqual(len(monitor.load_history()), 6)

    def test_excel(self):
        self.collect()
        wb = load_workbook(monitor.OUTPUT)
        self.addCleanup(wb.close)
        self.assertEqual(wb.sheetnames, ["Current", "Changes", "History", "Errors"])
        row = next(r for r in list(wb["Current"].values)[1:] if r[2] == "32485")
        self.assertEqual(row[4:7], (68.9, 74.8, -7.89))
        self.assertEqual(row[8:], (76.9, 10.4, True))
        history = list(wb["History"].values)[1:]
        self.assertEqual(len(history), 6)
        self.assertEqual(history[0][8:], (None, None, None))
        self.assertEqual(history[3][8:], (76.9, 10.4, True))

    def test_failure_does_not_publish_partial_history(self):
        before = monitor.HISTORY.read_bytes()
        with self.assertRaises(SystemExit) as raised:
            self.collect(fail=True)
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(monitor.HISTORY.read_bytes(), before)
        wb = load_workbook(monitor.OUTPUT)
        self.addCleanup(wb.close)
        self.assertEqual(wb["History"].max_row, 4)
        self.assertEqual(wb["Errors"].max_row, 2)

class AtbTests(unittest.TestCase):
    product = {"product": "Банан, кг", "sku": "29"}
    html = """
    <section class="product-page"><h1>Банан</h1>
    <span class="custom-tag__text"><strong data-available="1">29</strong></span>
    <div class="product-about__buy-row">
      <div class="product-price--unit"><data>12.00 грн/шт</data></div>
      <div class="product-price--weight product-price--sale">
        <data class="product-price__top"><span>60.<span>00</span></span>
          <abbr>грн<span>/кг</span></abbr></data>
        <data class="product-price__bottom"><span>75.<span>00</span></span></data>
      </div>
    </div></section>"""

    def test_weight_promo(self):
        self.assertEqual(monitor.parse_atb_prices(self.html, self.product),
                         dict(price="60.00", regular_price="75.00",
                              discount_pct="20.00", is_promo="true"))

    def test_reject_invalid_card(self):
        for before, after in [('>29<', '>129<'), ('/кг', '/шт'),
                              ('data-available="1"', 'data-available="0"'),
                              ('product-price--sale', '')]:
            with self.subTest(before=before), self.assertRaises(ValueError):
                monitor.parse_atb_prices(self.html.replace(before, after), self.product)


if __name__ == "__main__":
    unittest.main()
