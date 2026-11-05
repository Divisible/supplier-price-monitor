"""Collect public Fora prices and build a daily XLSX report.

Prices are accepted only from the first visible price element when the page
title matches the expected product. No fabricated values on failures.
"""
import csv
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "price_history.csv"
OUTPUT = ROOT / "output" / "Supplier_Prices.xlsx"
FIELDS = ["date", "checked_at", "supplier", "sku", "product", "currency", "price", "url"]
PRICE_PATTERN = re.compile(r"^\s*(\d{1,6})(?:[,.](\d{1,2}))?\s*грн\s*$", re.I)


def parse_price(raw):
    raw = raw.replace("\xa0", " ").strip()
    match = PRICE_PATTERN.fullmatch(raw)
    if not match:
        raise ValueError(f"Ambiguous price text: {raw!r}")
    major, minor = match.groups()
    # Fora's UI sometimes renders 7990 грн for 79.90 UAH.
    if minor is None:
        if len(major) < 3:
            raise ValueError(f"Unclear price format: {raw!r}")
        return Decimal(major) / 100
    return Decimal(major + "." + minor.ljust(2, "0"))


def load_history():
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def save_history(rows):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def make_report(history, errors):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    current = wb.active
    current.title = "Current"
    changes = wb.create_sheet("Changes")
    old = wb.create_sheet("History")
    failures = wb.create_sheet("Errors")
    header = ["Date", "Supplier", "SKU", "Product", "Price UAH", "Previous UAH", "Change %", "URL"]
    current.append(header)
    changes.append(header)
    old.append(["Date", "Checked at", "Supplier", "SKU", "Product", "Currency", "Price", "URL"])
    failures.append(["Supplier", "SKU", "Product", "Error", "URL"])
    grouped = {}
    for record in history:
        grouped.setdefault((record["supplier"], record["sku"]), []).append(record)
        old.append([record[k] for k in FIELDS])
    for records in grouped.values():
        records.sort(key=lambda r: (r["date"], r["checked_at"]))
        last = records[-1]
        previous = records[-2] if len(records) > 1 else None
        price = float(last["price"])
        prev_price = float(previous["price"]) if previous else None
        change = round((price / prev_price - 1) * 100, 2) if prev_price else None
        row = [last["date"], last["supplier"], last["sku"], last["product"], price, prev_price, change, last["url"]]
        current.append(row)
        if previous and change != 0:
            changes.append(row)
    for err in errors:
        failures.append([err["supplier"], err["sku"], err["product"], err["error"], err["url"]])
    for ws in wb:
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for cell in ws[1]:
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.font = Font(bold=True, color="FFFFFF")
        for column in "ABCDEFGH":
            ws.column_dimensions[column].width = 20 if column != "D" else 32
    wb.save(OUTPUT)


def main():
    with (ROOT / "products.csv").open(encoding="utf-8-sig", newline="") as stream:
        products = list(csv.DictReader(stream))
    history = load_history()
    errors = []
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today = now.date().isoformat()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(locale="uk-UA", timezone_id="Europe/Kyiv")
        for product in products:
            sku = product["sku"]
            try:
                response = page.goto(product["url"], wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                if not response or response.status != 200:
                    raise ValueError(f"HTTP status: {response.status if response else 'none'}")
                title = page.title().casefold()
                if product["product"].split(",")[0].casefold() not in title:
                    raise ValueError(f"Product title mismatch: {title[:120]}")
                # Restrict to the first visible price candidate. Fail closed if
                # markup changes; inspect screenshots before enabling scheduling.
                candidates = page.locator('[itemprop="price"], [class*="price"], [data-testid*="price"]')
                price = None
                for idx in range(min(candidates.count(), 12)):
                    node = candidates.nth(idx)
                    if not node.is_visible():
                        continue
                    raw = node.inner_text(timeout=3000).strip()
                    try:
                        price = parse_price(raw)
                        break
                    except ValueError:
                        continue
                if price is None or price <= 0:
                    raise ValueError("No unambiguous visible price found")
                record = dict(date=today, checked_at=now.isoformat(), supplier=product["supplier"],
                              sku=sku, product=product["product"], currency=product["currency"],
                              price=str(price.quantize(Decimal("0.01"))), url=product["url"])
                history = [r for r in history if not (r["date"] == today and r["supplier"] == product["supplier"] and r["sku"] == sku)]
                history.append(record)
                print(f"{sku}: {record['price']} UAH")
            except Exception as exc:
                errors.append(dict(supplier=product["supplier"], sku=sku, product=product["product"],
                                   url=product["url"], error=str(exc)))
                print(f"{sku}: ERROR {exc}")
        browser.close()
    history.sort(key=lambda r: (r["date"], r["supplier"], r["sku"]))
    save_history(history)
    make_report(history, errors)
    print(f"Report: {OUTPUT}; errors: {len(errors)}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
