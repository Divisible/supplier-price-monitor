"""Render SPM daily price notes locally with Pillow. Run --help for CLI options."""
import argparse
import csv
import os
from datetime import date, datetime
from pathlib import Path
import tomllib

from PIL import Image, ImageDraw, ImageFont

from app import HISTORY, KEY, KYIV, daily_changes, load_history
from spm_ui import SUPPLIER_STYLES

ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 1600, 900
ROW_WIDTH = 674


def find_font(explicit=None):
    candidates = [explicit] if explicit else [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return str(path)
    raise ValueError("Нужен локальный шрифт с кириллицей. Укажите --font /path/to/font.ttf")


def wrap_text(text, font, width):
    lines = []
    line = ""
    for word in str(text).split():
        candidate = f"{line} {word}".strip()
        if font.getlength(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and font.getlength(line + char) > width:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines or [""]


def amount(value):
    return f"{value:.2f}".replace(".", ",")


def layout_rows(rows, font):
    """Pack each column independently, with six rows and 558 pixels at most."""
    pages, page, height = [], [], 0
    for row in rows:
        lines = wrap_text(row.product, font, ROW_WIDTH)
        size = max(89, len(lines) * 27 + 58)
        if size > 558:
            raise ValueError(f"Название SKU {row.sku} слишком длинное для страницы 1600×900.")
        if len(page) == 6 or height + size > 558:
            pages.append(page)
            page, height = [], 0
        page.append((row, lines, size))
        height += size
    if page:
        pages.append(page)
    return pages


def collection_summary(path):
    if not path.exists():
        return "Последний сбор · диагностика недоступна"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set([*KEY, "checked_at", "status"]).issubset(reader.fieldnames or []):
            raise ValueError("Некорректная структура диагностики сбора")
        rows = list(reader)
    if not rows:
        return "Последний сбор · диагностика пуста"
    if len({(r['supplier'], r['sku']) for r in rows}) != len(rows):
        raise ValueError("В диагностике повторяются поставщик и SKU")
    timestamps = [datetime.fromisoformat(r["checked_at"]) for r in rows]
    if any(t.tzinfo is None for t in timestamps):
        raise ValueError("В диагностике отсутствует часовой пояс")
    last = max(timestamps).astimezone(KYIV).strftime("%d.%m.%Y %H:%M")
    counts = [sum(r["status"] == status for r in rows) for status in ("error", "out_of_stock", "price_missing")]
    return (f"Последние статусы · проверено до {last} по Киеву   |   "
            f"Ошибки {counts[0]}   ·   Нет в наличии {counts[1]}   ·   Без цены {counts[2]}")


def render_notes(changes, comparable, report_date, output, footer, font_path):
    with (ROOT / ".streamlit/config.toml").open("rb") as stream:
        theme = tomllib.load(stream)["theme"]
    font = lambda size: ImageFont.truetype(font_path, size)
    normal, small, title = font(23), font(20), font(36)
    sides = [changes.loc[changes["change"].gt(0)], changes.loc[changes["change"].lt(0)]]
    packed = [
        layout_rows(side.sort_values("change_pct", key=abs, ascending=False, kind="stable").itertuples(index=False), normal)
        if not side.empty else []
        for side in sides
    ]
    count = max(1, *(len(pages) for pages in packed))
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    for number in range(count):
        canvas = Image.new("RGB", (WIDTH, HEIGHT), theme["backgroundColor"])
        draw = ImageDraw.Draw(canvas)
        def text(x, y, value, face=normal, color=None):
            draw.text((x, y), value, font=face, fill=color or theme["textColor"], anchor="lt")

        text(48, 38, "SPM", title, theme["primaryColor"])
        text(145, 45, "|  DAILY PRICE NOTES", font(28))
        header = f"{report_date:%d.%m.%Y} · Киев   |   {number + 1} / {count}"
        text(WIDTH - 48 - small.getlength(header), 48, header, small)
        text(48, 102, f"Сравнено {comparable}   ·   Изменений {len(changes)}   ·   "
             f"Подорожали {len(sides[0])}   ·   Подешевели {len(sides[1])}", small)
        if not changes.empty:
            for side, x in enumerate((48, 816)):
                color = theme["redTextColor" if side == 0 else "greenTextColor"]
                draw.rounded_rectangle((x, 150, x + 736, 792), radius=18, fill=theme["secondaryBackgroundColor"])
                text(x + 26, 173, "PRICE NERFS" if side == 0 else "PRICE BUFFS", font(27), color)
                text(x + 266, 180, "/ ПОДОРОЖАЛИ" if side == 0 else "/ ПОДЕШЕВЕЛИ", small, color)
                y = 225
                entries = packed[side][number] if number < len(packed[side]) else []
                if not entries:
                    text(x + 26, y + 30, "На этой странице изменений нет", small, theme["grayTextColor"])
                for row, lines, height in entries:
                    # Draw arrows as geometry so glyph support does not affect direction.
                    tip = y + (2 if side == 0 else 21)
                    base = y + (13 if side == 0 else 10)
                    draw.polygon([(x + 25, tip), (x + 17, base), (x + 33, base)], fill=color)
                    draw.line((x + 25, y + 8, x + 25, y + 19), fill=color, width=3)
                    for index, line in enumerate(lines):
                        text(x + 44, y + index * 27, line)
                    baseline = y + len(lines) * 27 + 7
                    supplier_color = SUPPLIER_STYLES.get(row.supplier, (theme["grayTextColor"],))[0]
                    text(x + 44, baseline, row.supplier, small, supplier_color)
                    prices = f"{amount(row.previous_price)} → {amount(row.price)} грн"
                    percent = f"{row.change_pct:+.2f}%".replace(".", ",")
                    price_font = small
                    available = 440 - small.getlength(percent) - 24
                    while price_font.getlength(prices) > available and price_font.size > 12:
                        price_font = font(price_font.size - 1)
                    if price_font.getlength(prices) > available:
                        raise ValueError("Значение цены не помещается в строку отчёта")
                    text(x + 260, baseline, prices, price_font)
                    text(x + 710 - small.getlength(percent), baseline, percent, small, color)
                    y += height
                    draw.line((x + 26, y - 10, x + 710, y - 10), fill="#E2E8F0")
        if changes.empty:
            draw.rounded_rectangle((48, 150, 1552, 792), radius=18, fill=theme["secondaryBackgroundColor"])
            message = "Недостаточно данных для сравнения" if not comparable else "Изменений цен нет"
            text((WIDTH - title.getlength(message)) / 2, 350, message, title)
            detail = "Нет сопоставимых наблюдений за дату отчёта и предыдущий день" if not comparable else "Цены совпадают с предыдущим днём"
            text((WIDTH - small.getlength(detail)) / 2, 410, detail, small)
        text(48, 819, footer, small)
        text(48, 856, "Состояние последних попыток сбора · статусы не являются новыми событиями за день", font(17), theme["grayTextColor"])
        path = output / f"spm-price-notes-{report_date.isoformat()}-{number + 1:02d}.png"
        canvas.save(path)
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(KYIV).date(), help="YYYY-MM-DD, по Киеву")
    parser.add_argument("--output", type=Path, required=True, help="Выходная папка PNG")
    parser.add_argument("--font", type=Path, help="Локальный TTF/OTF с кириллицей")
    args = parser.parse_args()
    try:
        history = load_history()
        if history.empty:
            import pandas as pd
            changes, comparable = pd.DataFrame(columns=[*KEY, "change"]), 0
        else:
            changes, comparable = daily_changes(history, args.date)
        footer = collection_summary(HISTORY.with_name("collection_status.csv"))
        for path in render_notes(changes, comparable, args.date, args.output, footer, find_font(args.font)):
            print(path.resolve())
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(1, f"Не удалось создать отчёт: {exc}\n")


if __name__ == "__main__":
    main()
