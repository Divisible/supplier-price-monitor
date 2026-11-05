"""Pilot: capture rendered information for manual price validation."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from openpyxl import load_workbook
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
rows = list(load_workbook(ROOT / "products.xlsx", read_only=True, data_only=True)["Товары"].values)[1:]
results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(locale="uk-UA", timezone_id="Europe/Kyiv", viewport={"width": 1365, "height": 900})
    for row in rows:
        supplier, sku, name, url, *_ = row
        if not url:
            continue
        result = {
            "checked_at": datetime.now(ZoneInfo("Europe/Kyiv")).isoformat(),
            "supplier": supplier, "sku": str(sku), "product": name, "url": url
        }
        try:
            response = page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            result["http_status"] = response.status if response else None
            result["title"] = page.title()
            result["rendered_text"] = page.locator("body").inner_text(timeout=10000)[:10000]
            result["price_candidates"] = page.locator(
                '[itemprop="price"], [class*="price"], [data-testid*="price"]'
            ).all_text_contents()[:30]
            page.screenshot(path=str(ROOT / f"{sku}.png"), full_page=False)
            result["status"] = "NEEDS_REVIEW"
        except Exception as exc:
            result["status"] = "ERROR"
            result["error"] = str(exc)
        results.append(result)
    browser.close()
(ROOT / "diagnostics.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"Checked {len(results)} products; see diagnostics.json and screenshots.")
