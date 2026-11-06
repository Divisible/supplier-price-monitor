"""Collect public Fora prices and build a daily XLSX report.

Prices are accepted only from an unambiguous main-product price block when the
page title matches the expected product. No fabricated values on failures.
"""
import csv
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "price_history.csv"
OUTPUT = ROOT / "output" / "Supplier_Prices.xlsx"
LEGACY_FIELDS = ["date", "checked_at", "supplier", "sku", "product", "currency", "price", "url"]
FIELDS = LEGACY_FIELDS + ["regular_price", "discount_pct", "is_promo"]
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



def promo_fields(price, regular=None):
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid selling price")
    if regular is None:
        return dict(regular_price="", discount_pct="0", is_promo="false")
    if not regular.is_finite() or regular <= price:
        raise ValueError("Regular price must exceed selling price")
    discount = ((regular - price) / regular * 100).quantize(Decimal("0.01"))
    return dict(regular_price=str(regular), discount_pct=str(discount), is_promo="true")


def parse_product_prices(details, sku):
    # Anchor to the SKU/unit and require the complete price block before the
    # purchase button. Additional price candidates or unfamiliar text fail closed.
    normalized = details.replace("\xa0", " ").strip()
    if len(re.findall(r"(?<!\d)" + re.escape(sku) + r"(?!\d)", normalized)) != 1:
        raise ValueError("Missing or ambiguous product SKU")
    tail = normalized.split(sku, 1)[1]
    block, separator, _ = tail.partition("В кошик")
    if not separator:
        raise ValueError("Missing purchase button after price")
    pattern = (
        r"\s*(?:кг|шт|л|г)\s*\n"
        r"(?:\s*(?P<regular>\d{1,5}[,.]\d{2})[ \t]*грн\s*\n"
        r"\s*[-−]\d{1,2}(?:[,.]\d{1,2})?%\s*\n)?"
        r"\s*(?P<major>\d{1,5})[ \t]*\n"
        r"\s*(?P<minor>\d{2})[ \t]*грн\s*"
    )
    match = re.fullmatch(pattern, block, re.I)
    if not match:
        raise ValueError(f"Ambiguous main product price block: {block[:180]!r}")
    price = Decimal(match["major"] + "." + match["minor"])
    regular = Decimal(match["regular"].replace(",", ".")) if match["regular"] else None
    return dict(price=str(price), **promo_fields(price, regular))


def load_history():
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames not in (LEGACY_FIELDS, FIELDS):
            raise ValueError("Invalid history header")
        legacy = reader.fieldnames == LEGACY_FIELDS
        rows = list(reader)
    keys = set()
    for row in rows:
        if None in row or any(row.get(field) is None for field in reader.fieldnames) or not all(row.get(field) for field in LEGACY_FIELDS):
            raise ValueError("Incomplete history row")
        date.fromisoformat(row["date"])
        price = Decimal(row["price"])
        key = history_key(row)
        if not price.is_finite() or price <= 0 or key in keys:
            raise ValueError(f"Invalid or duplicate history row: {key}")
        if legacy:
            # Previous collections did not record whether an offer was promotional.
            row.update(regular_price="", discount_pct="", is_promo="")
        else:
            extra = {field: row[field] for field in FIELDS if field not in LEGACY_FIELDS}
            if extra != dict(regular_price="", discount_pct="", is_promo=""):
                regular = Decimal(row["regular_price"]) if row["regular_price"] else None
                if extra != promo_fields(price, regular):
                    raise ValueError(f"Inconsistent promotion fields: {key}")
        keys.add(key)
    return rows


def history_key(row):
    return row["date"], row["supplier"], row["sku"]


def save_history(rows):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(HISTORY)



def report_promo(record):
    regular = float(record["regular_price"]) if record["regular_price"] else None
    discount = float(record["discount_pct"]) if record["discount_pct"] else None
    promo = record["is_promo"] == "true" if record["is_promo"] else None
    return [regular, discount, promo]


def make_report(history, errors):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    current = wb.active
    current.title = "Current"
    changes = wb.create_sheet("Changes")
    old = wb.create_sheet("History")
    failures = wb.create_sheet("Errors")
    header = ["Date", "Supplier", "SKU", "Product", "Price UAH", "Previous UAH", "Change %", "URL"]
    current.append(header + ["Regular price UAH", "Discount %", "Is promo"])
    changes.append(header)
    old.append(["Date", "Checked at", "Supplier", "SKU", "Product", "Currency", "Price", "URL", "Regular price UAH", "Discount %", "Is promo"])
    failures.append(["Supplier", "SKU", "Product", "Error", "URL"])
    grouped = {}
    for record in history:
        grouped.setdefault((record["supplier"], record["sku"]), []).append(record)
        old.append([record[k] for k in LEGACY_FIELDS] + report_promo(record))
    for records in grouped.values():
        records.sort(key=lambda r: (r["date"], r["checked_at"]))
        last = records[-1]
        previous = records[-2] if len(records) > 1 else None
        price = float(last["price"])
        prev_price = float(previous["price"]) if previous else None
        change = round((price / prev_price - 1) * 100, 2) if prev_price else None
        row = [last["date"], last["supplier"], last["sku"], last["product"], price, prev_price, change, last["url"]]
        current.append(row + report_promo(last))
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
        for column in "ABCDEFGHIJK":
            ws.column_dimensions[column].width = 20 if column != "D" else 32
    wb.save(OUTPUT)


def main():
    with (ROOT / "products.csv").open(encoding="utf-8-sig", newline="") as stream:
        products = list(csv.DictReader(stream))
    history = load_history()
    keys = {history_key(row) for row in history}
    additions = []
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
                # Scope extraction to the product detail section, never site-wide
                # recommendations or other cards.
                details = page.locator("h1").first.evaluate("""(heading, sku) => {
                    let node = heading;
                    for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
                        const text = node.innerText || "";
                        if (text.includes(sku) && text.length < 2500 && /грн/.test(text)) {
                            return text;
                        }
                    }
                    return null;
                }""", sku)
                if not details:
                    raise ValueError("Cannot isolate product details containing title, SKU and price")
                prices = parse_product_prices(details, sku)
                record = dict(date=today, checked_at=now.isoformat(), supplier=product["supplier"],
                              sku=sku, product=product["product"], currency=product["currency"],
                              url=product["url"], **prices)
                key = history_key(record)
                if key not in keys:
                    additions.append(record)
                    keys.add(key)
                print(f"{sku}: {record['price']} UAH; regular={record['regular_price']}; "
                      f"discount={record['discount_pct']}%; promo={record['is_promo']}")
            except Exception as exc:
                errors.append(dict(supplier=product["supplier"], sku=sku, product=product["product"],
                                   url=product["url"], error=str(exc)))
                print(f"{sku}: ERROR {exc}")
        browser.close()
    if not errors:
        history.extend(additions)
        history.sort(key=history_key)
        # Also migrate a legacy file when all observations already exist today.
        save_history(history)
    make_report(history, errors)
    print(f"Report: {OUTPUT}; errors: {len(errors)}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
