"""Collect public supplier prices and build a daily XLSX report.

Prices are accepted only from an unambiguous main-product price block when the
page title matches the expected product. No fabricated values on failures.
"""
import csv
import json
import os
import re
from contextlib import ExitStack
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "data" / "price_history.csv"
OUTPUT = ROOT / "output" / "Supplier_Prices.xlsx"
LEGACY_FIELDS = ["date", "checked_at", "supplier", "sku", "product", "currency", "price", "url"]
FIELDS = LEGACY_FIELDS + ["regular_price", "discount_pct", "is_promo"]
STATUS_FIELDS = ["supplier", "sku", "checked_at", "status", "reason"]


class PriceMissing(ValueError):
    """A verified product card does not provide a usable price."""


class OutOfStock(ValueError):
    """The identified product has an explicit unavailable flag."""


class ForaCardError(ValueError):
    """Carries a fixed diagnostic code safe to write to collection_status.csv."""


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
        raise PriceMissing("Invalid selling price")
    if regular is None:
        return dict(regular_price="", discount_pct="0", is_promo="false")
    if not regular.is_finite() or regular <= price:
        raise PriceMissing("Regular price must exceed selling price")
    discount = ((regular - price) / regular * 100).quantize(Decimal("0.01"))
    return dict(regular_price=str(regular), discount_pct=str(discount), is_promo="true")


def parse_product_prices(details, sku, expected_unit=None):
    # Anchor to the SKU/unit and require the complete price block before the
    # purchase button. Additional price candidates or unfamiliar text fail closed.
    normalized = details.replace("\xa0", " ").strip()
    sku_line = re.search(r"(?m)^[ \t]*" + re.escape(sku) + r"[ \t]*$", normalized)
    if not sku_line:
        raise ForaCardError("SKU_NOT_FOUND")
    if len(re.findall(r"(?<!\d)" + re.escape(sku) + r"(?!\d)", normalized)) != 1:
        raise ForaCardError("SKU_AMBIGUOUS")
    if "Артикул:" in normalized and not normalized[:sku_line.start()].rstrip().endswith("Артикул:"):
        raise ForaCardError("SKU_NOT_FOUND")
    tail = normalized[sku_line.end():]
    if re.search(r"(?im)^\s*(немає в наявності|відсутній у продажу)\s*$", normalized):
        raise OutOfStock()
    block, separator, _ = tail.partition("В кошик")
    if not separator:
        raise PriceMissing()
    pattern = (
        r"\s*(?P<unit>кг|шт|л|г)\s*\n"
        r"(?:\s*(?P<regular>\d{1,5}[,.]\d{2})[ \t]*грн\s*\n"
        r"\s*[-−]\d{1,2}(?:[,.]\d{1,2})?%\s*\n)?"
        r"\s*(?P<major>\d{1,5})[ \t]*\n"
        r"\s*(?P<minor>\d{2})[ \t]*грн\s*"
    )
    match = re.fullmatch(pattern, block, re.I)
    if not match:
        raise PriceMissing()
    if expected_unit and match["unit"].casefold() != expected_unit:
        raise ForaCardError("UNIT_MISMATCH")
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


def save_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", newline="", delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)



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


def collect_fora(page, product):
    sku = product["sku"]
    response = page.goto(product["url"], wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(6000)
    if not response or response.status != 200:
        raise ForaCardError("HTTP_ERROR")
    title = page.title().casefold()
    if product["product"].split(",")[0].casefold() not in title:
        raise ForaCardError("PRODUCT_TITLE_MISMATCH")
    # Scope extraction to the product detail section, never site-wide
    # recommendations or other cards.
    details = page.locator("h1").first.evaluate("""(heading, sku) => {
        let node = heading;
        for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
            const text = node.innerText || "";
            if (text.includes(sku) && text.length < 2500 && /грн|немає в наявності|відсутній у продажу/i.test(text)) {
                return text;
            }
        }
        return null;
    }""", sku)
    if not details:
        # Missing SKU can also indicate a broken or unrelated product page.
        if not page.get_by_text(sku, exact=True).count():
            raise ForaCardError("SKU_NOT_FOUND")
        raise PriceMissing()
    _, _, unit = product["product"].rpartition(", ")
    return parse_product_prices(details, sku, unit or None)


def single_element(parent, selector):
    elements = parent.select(selector)
    if len(elements) != 1:
        raise ValueError(f"Expected one {selector}, found {len(elements)}")
    return elements[0]


def catalog_title_and_unit(product):
    # A catalog name may end in an explicit sales unit; older unsuffixed names are per kg.
    title, _, unit = product["product"].rpartition(", ")
    if title and unit in ("кг", "шт"):
        return title, unit
    return product["product"], "кг"


def parse_atb_prices(html, product):
    soup = BeautifulSoup(html, "html.parser")
    card = single_element(soup, "section.product-page")
    title = single_element(card, "h1").get_text(strip=True)
    expected_title, expected_unit = catalog_title_and_unit(product)
    if title.casefold() != expected_title.casefold():
        raise ValueError(f"ATB product mismatch: {title}")
    code = single_element(card, ".custom-tag__text strong[data-available]")
    if code.get_text(strip=True) != product["sku"]:
        raise ValueError("ATB SKU mismatch")
    if code["data-available"] == "0":
        raise OutOfStock()
    if code["data-available"] != "1":
        raise ValueError("Unknown ATB availability")
    try:
        return atb_product_price(card, expected_unit)
    except ValueError as exc:
        raise PriceMissing() from exc


def atb_product_price(card, expected_unit):
    block = single_element(card, ".product-about__buy-row .product-price--weight")
    current = single_element(block, "data.product-price__top")
    unit = single_element(current, "abbr").get_text(strip=True)
    if unit != f"грн/{expected_unit}":
        raise ValueError(f"Unexpected ATB unit: {unit}")

    def amount(tag):
        raw = single_element(tag, ":scope > span").get_text(strip=True)
        if not re.fullmatch(r"[0-9]{1,5}[.,][0-9]{2}", raw):
            raise ValueError(f"Ambiguous ATB amount: {raw}")
        return Decimal(raw.replace(",", "."))

    price = amount(current)
    sale = "product-price--sale" in block.get("class", [])
    if len(block.select("data")) != (2 if sale else 1):
        raise ValueError("Ambiguous ATB price block")
    regular = amount(single_element(block, "data.product-price__bottom")) if sale else None
    return dict(price=str(price), **promo_fields(price, regular))


def collect_varus(product):
    slug = urlsplit(product["url"]).path.strip("/")
    url = f"https://varus.ua/kyiv/{slug}"
    html = fetch_html(url)
    marker = "window.__INITIAL_STATE__="
    if marker not in html:
        raise ValueError("Missing VARUS product state")
    state, _ = json.JSONDecoder(parse_float=Decimal).raw_decode(html.split(marker, 1)[1].lstrip())
    item = state["product"]["current"]
    expected_title, expected_unit = catalog_title_and_unit(product)
    if (state["route"]["path"].rstrip("/") != f"/kyiv/{slug}"
            or str(item["sku"]) != product["sku"] or item["name"] != expected_title
            or item["unit"] != {"кг": "kg", "шт": "pc"}[expected_unit]):
        raise ValueError("VARUS city, product, unit or availability mismatch")
    if item["stock"]["is_in_stock"] is False:
        raise OutOfStock()
    if item["stock"]["is_in_stock"] is not True:
        raise ValueError("Unknown VARUS availability")
    soup = BeautifulSoup(html, "html.parser")
    schemas = [json.loads(tag.get_text(), parse_float=Decimal)
               for tag in soup.select('script[type="application/ld+json"]')]
    schema, = [s for s in schemas if isinstance(s, dict) and s.get("@type") == "Product"]
    offer = schema["offers"]
    if str(schema["sku"]) != product["sku"] or not offer["availability"].endswith("/InStock"):
        raise ValueError("VARUS offer mismatch")
    try:
        price = Decimal(item["final_price"])
        regular = Decimal(item["regular_price"])
        if (offer.get("priceCurrency") != product["currency"] or Decimal(offer["price"]) != price
                or not regular.is_finite() or regular < price):
            raise PriceMissing()
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise PriceMissing() from exc
    return dict(price=format(price, ".2f"), **promo_fields(price, regular if regular > price else None))


def fetch_html(url):
    request = Request(url, headers={"User-Agent": "supplier-price-monitor/1.0"})
    with urlopen(request, timeout=45) as response:
        return response.read().decode("utf-8")


def collect_atb(product):
    return parse_atb_prices(fetch_html(product["url"]), product)


def main():
    with (ROOT / "products.csv").open(encoding="utf-8-sig", newline="") as stream:
        products = list(csv.DictReader(stream))
    history = load_history()
    by_day = {history_key(row): row for row in history}
    attempts = []
    errors = []
    changed = False
    with ExitStack() as stack:
        page = None
        for product in products:
            supplier = product["supplier"]
            sku = product["sku"]
            prices = None
            status, reason = "ok", ""
            try:
                if supplier == "АТБ":
                    prices = collect_atb(product)
                elif supplier == "Варус":
                    prices = collect_varus(product)
                elif supplier == "Фора":
                    if page is None or page.is_closed() is True:
                        playwright = stack.enter_context(sync_playwright())
                        browser = playwright.chromium.launch(headless=True)
                        page = browser.new_page(locale="uk-UA", timezone_id="Europe/Kyiv")
                    prices = collect_fora(page, product)
                else:
                    raise ValueError("Unsupported supplier")
            except OutOfStock:
                status = "out_of_stock"
                reason = "OUT_OF_STOCK" if supplier == "Фора" else "Источник подтвердил отсутствие товара."
            except PriceMissing:
                status = "price_missing"
                reason = "PRICE_BLOCK_MISSING_OR_INVALID" if supplier == "Фора" else "Цена в карточке отсутствует или не прошла проверку."
            except ForaCardError as exc:
                status, reason = "error", str(exc)
            except HTTPError as exc:
                status, reason = "error", f"HTTP {exc.code}"
            except (URLError, TimeoutError):
                status, reason = "error", "Ошибка соединения или истекло время ожидания."
            except Exception as exc:
                # Exception messages from browsers may contain raw HTML or signed URLs.
                status, reason = "error", f"Ошибка запроса или разбора ({type(exc).__name__})."

            now = datetime.now(ZoneInfo("Europe/Kyiv"))
            checked_at = now.isoformat()
            attempts.append(dict(supplier=supplier, sku=sku, checked_at=checked_at, status=status, reason=reason))
            if status == "ok":
                record = dict(date=now.date().isoformat(), checked_at=checked_at, supplier=supplier,
                              sku=sku, product=product["product"], currency=product["currency"],
                              url=product["url"], **prices)
                key = history_key(record)
                previous = by_day.get(key)
                if previous is None or (now > datetime.fromisoformat(previous["checked_at"])
                                        and any(previous[k] != v for k, v in prices.items())):
                    by_day[key] = record
                    changed = True
            else:
                errors.append(dict(supplier=supplier, sku=sku, product=product["product"],
                                   url=product["url"], error=reason))
            print(f"{supplier} {sku}: {status} {reason}")

    if changed:
        history = sorted(by_day.values(), key=history_key)
        save_csv(HISTORY, FIELDS, history)
    save_csv(HISTORY.with_name("collection_status.csv"), STATUS_FIELDS, attempts)
    make_report(history, errors)
    print(f"Successful: {len(products) - len(errors)}/{len(products)}")
    if len(errors) == len(products):
        print("No product yielded a verified price.")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
