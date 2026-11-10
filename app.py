"""Read-only price history dashboard. Run with streamlit run app.py."""
import csv
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st
from spm_ui import dashboard_layout, metric_amount, metric_cards, price_tooltip, style_chart, styled_table, supplier_scales

HISTORY = Path(__file__).resolve().parent / "data" / "price_history.csv"
KYIV = ZoneInfo("Europe/Kyiv")
KEY = ["supplier", "sku"]
REQUIRED = ["date", "checked_at", "supplier", "sku", "product", "currency", "price", "url"]
PROMO = ["regular_price", "discount_pct", "is_promo"]
LABELS = {
    "supplier": "Поставщик", "sku": "SKU", "product": "Товар",
    "date": "Дата", "checked_at": "Проверено по Киеву", "price": "Цена, грн",
    "previous_price": "Предыдущая, грн", "change": "Изменение, грн",
    "change_pct": "Изменение, %", "regular_price": "Обычная, грн",
    "discount_pct": "Скидка, %", "is_promo": "Акция", "url": "Карточка товара",
}


def load_history(path=HISTORY):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        fields = reader.fieldnames
        if fields is None:
            return pd.DataFrame()
        if len(fields) != len(set(fields)) or not set(REQUIRED).issubset(fields):
            raise ValueError("В CSV отсутствуют обязательные столбцы или есть повторные заголовки.")
        rows = list(reader)
    if not rows:
        return pd.DataFrame()
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("Число значений в строках CSV не совпадает с заголовком.")
    data = pd.DataFrame(rows).apply(lambda column: column.str.strip())
    if data[REQUIRED].eq("").any().any():
        raise ValueError("В обязательных полях CSV есть пустые значения.")
    for field in PROMO:
        if field not in data:
            data[field] = ""
    try:
        timestamps = [datetime.fromisoformat(value) for value in data["checked_at"]]
        if any(value.tzinfo is None for value in timestamps):
            raise ValueError("Время наблюдения должно содержать часовой пояс.")
        data["checked_at"] = pd.to_datetime(timestamps, utc=True).tz_convert(KYIV)
        data["date"] = pd.to_datetime(data["date"], format="%Y-%m-%d", errors="raise")
    except (ValueError, TypeError) as exc:
        raise ValueError("Некорректная дата или время наблюдения в CSV.") from exc
    if (data["date"].dt.date != data["checked_at"].dt.date).any():
        raise ValueError("Дата записи не совпадает с календарной датой проверки по Киеву.")
    for field in ["price", "regular_price", "discount_pct"]:
        supplied = data[field].ne("")
        numeric = pd.to_numeric(data[field].replace("", pd.NA), errors="coerce")
        if (supplied & (numeric.isna() | numeric.isin([float("inf"), -float("inf")]))).any():
            raise ValueError(f"Некорректное число в поле {field}.")
        data[field] = numeric.astype("Float64")
    if (data["price"] <= 0).any() or (data["regular_price"].dropna() <= 0).any():
        raise ValueError("Цены должны быть положительными.")
    if not data["currency"].eq("UAH").all():
        raise ValueError("Приложение поддерживает цены в UAH.")
    flags = data["is_promo"].str.lower()
    if not flags.isin(["", "true", "false"]).all():
        raise ValueError("Поле is_promo должно содержать true, false или пустое значение.")
    data["is_promo"] = flags.map({"true": True, "false": False}).astype("boolean")
    known_regular = data["regular_price"].notna()
    calculated = ((data["regular_price"] - data["price"]) / data["regular_price"] * 100).round(2)
    if (known_regular & (data["regular_price"] <= data["price"])).any():
        raise ValueError("Зачёркнутая цена должна превышать фактическую.")
    if (data["is_promo"].eq(True).fillna(False) & ~known_regular).any():
        raise ValueError("Для промоакции отсутствует обычная цена.")
    if (data["is_promo"].eq(False).fillna(False) & known_regular).any():
        raise ValueError("Обычная цена противоречит признаку акции.")
    stated = data["discount_pct"].notna()
    if (known_regular & stated & ((data["discount_pct"] - calculated).abs() > 0.011)).any():
        raise ValueError("Скидка в CSV не соответствует двум ценам.")
    if (~known_regular & stated & data["discount_pct"].ne(0)).any():
        raise ValueError("Для скидки отсутствует обычная цена.")
    data["reported_discount"] = data["discount_pct"].copy()
    # Recalculate only when both actual and regular prices are known.
    data.loc[known_regular, "discount_pct"] = calculated[known_regular]
    daily_key = [*KEY, "date"]
    return (data.sort_values([*daily_key, "checked_at"], kind="stable")
            .drop_duplicates(daily_key, keep="last").reset_index(drop=True))


def latest_prices(data):
    result = data.sort_values([*KEY, "date", "checked_at"]).copy()
    result["previous_price"] = result.groupby(KEY)["price"].shift()
    result["change"] = result["price"] - result["previous_price"]
    result["change_pct"] = (result["change"] / result["previous_price"] * 100).round(2)
    return result.groupby(KEY, sort=False).tail(1).reset_index(drop=True)


def filter_history(data, supplier, sku, days, today):
    selected = data.loc[data["supplier"].eq(supplier) & data["sku"].eq(sku)].copy()
    if days is not None:
        first = today - timedelta(days=days - 1)
        selected = selected.loc[selected["date"].dt.date.between(first, today)]
    return selected.sort_values(["date", "checked_at"])


def show_table(data, columns, extra_prices=()):
    display = data[columns].copy()
    if "is_promo" in display:
        display["is_promo"] = display["is_promo"].map({True: "Да", False: "Нет"}).fillna("Неизвестно")
    config = {"date": st.column_config.DateColumn("Дата", format="DD.MM.YYYY"),
              "checked_at": st.column_config.DatetimeColumn("Проверено по Киеву", timezone="Europe/Kyiv",
                                                          format="DD.MM.YYYY HH:mm")}
    money = [k for k in ["price", "regular_price", "previous_price", *extra_prices] if k in display]
    for field in [*money, "change", "discount_pct", "change_pct"]:
        config[field] = st.column_config.NumberColumn(LABELS.get(field, field))
    config["url"] = st.column_config.LinkColumn("Карточка товара", display_text="Открыть")
    for field in columns:
        config.setdefault(field, LABELS.get(field, field))
    styled_table(display, config, money=money, percent=["discount_pct"] if "discount_pct" in display else [])


PRICE_LABELS = {
    "canonical_name": "Главный товар", "supplier": "Поставщик", "sku": "Артикул", "product": "Товар",
    "normal_price": "Цена обычная, грн", "promo_price": "Цена в акцию, грн",
    "promo_difference": "Разница, грн", "reported_discount": "% скидки",
}
PRICE_FIELDS = ["normal_price", "promo_price", "promo_difference", "reported_discount"]


def table_prices(rows):
    rows = rows.copy()
    promo = rows["is_promo"].eq(True).fillna(False)
    rows["normal_price"] = rows["price"].where(~promo, rows["regular_price"])
    rows["promo_price"] = rows["price"].where(promo)
    rows["promo_difference"] = rows["promo_price"] - rows["normal_price"]
    rows["reported_discount"] = rows["reported_discount"].where(promo)
    return rows


def show_price_table(rows, overview=False):
    config = {field: st.column_config.NumberColumn(PRICE_LABELS[field]) for field in PRICE_FIELDS}
    formats = {}
    if overview:
        columns = ["canonical_name", *KEY, "url", *PRICE_FIELDS]
        config.update({field: PRICE_LABELS[field] for field in ["canonical_name", *KEY]})
        config["url"] = st.column_config.LinkColumn("Товар")
        formats["url"] = dict(zip(rows["url"], rows["product"])).get
    else:
        columns = ["date", "checked_at", *PRICE_FIELDS, "url"]
        config.update(date=st.column_config.DateColumn("Дата", format="DD.MM.YYYY"),
                      checked_at=st.column_config.DatetimeColumn("Проверено по Киеву", timezone="Europe/Kyiv",
                                                                format="DD.MM.YYYY HH"),
                      url=st.column_config.LinkColumn("Карточка товара", display_text="Открыть"))
    styled_table(rows[columns], config, money=PRICE_FIELDS[:3], percent=["reported_discount"],
                 formats=formats, height="content" if overview else "auto")


def page_rows(rows, size, key):
    count = max(1, math.ceil(len(rows) / size))
    page = min(st.session_state.get(key, 1), count)
    st.session_state[key] = page
    if count > 1:
        with st.container(horizontal=True, gap="small"):
            def button(label, target, disabled=False, active=False):
                st.button(label, key=f"{key}_{label}", disabled=disabled,
                          type="primary" if active else "secondary",
                          on_click=st.session_state.update, args=({key: target},))

            button("‹ Назад", page - 1, page == 1)
            previous = 0
            for n in sorted({1, count, *range(max(1, page - 1), min(count, page + 1) + 1)}):
                if n > previous + 1:
                    st.text("…", width="content")
                button(str(n), n, active=n == page)
                previous = n
            button("Вперёд ›", page + 1, page == count)
    return rows.iloc[(page - 1) * size:page * size]


def show_current_prices(catalog, fresh, mapping):
    fields = [*KEY, "price", "is_promo", "regular_price", "reported_discount"]
    current = catalog.merge(fresh.reindex(columns=fields), on=KEY, how="left", validate="one_to_one")
    if mapping is None:
        current["canonical_name"] = pd.NA
    else:
        current = current.merge(mapping[[*KEY, "canonical_name"]], on=KEY, how="left")
    current = table_prices(current)
    search, sort, direction = st.columns([2, 1.3, 1.3], vertical_alignment="bottom")
    query = search.text_input("Поиск по товару, поставщику или артикулу", on_change=st.session_state.update,
                          args=({"overview_page": 1},)).strip()
    if query:
        matches = current[["product", *KEY]].apply(
            lambda col: col.str.casefold().str.contains(query.casefold(), regex=False, na=False))
        current = current.loc[matches.any(axis=1)]
    field = sort.selectbox("Сортировать по", list(PRICE_LABELS), index=1, format_func=PRICE_LABELS.get)
    order = direction.selectbox("Порядок", ["По возрастанию", "По убыванию"])
    if current.empty:
        st.info("Ничего не найдено")
        return
    current = current.sort_values(field, ascending=order == "По возрастанию", kind="stable")
    table = st.container()
    page = page_rows(current, 15, "overview_page")
    with table:
        show_price_table(page, overview=True)


def daily_axis(dates, days, today):
    first = today - timedelta(days=days - 1) if days else dates.min().date()
    last = today if days else dates.max().date()
    ticks = [alt.DateTime(year=d.year, month=d.month, date=d.day, utc=True)
             for d in pd.date_range(first, max(last, first + timedelta(days=1)))]
    return alt.X("date:T", title="Дата", scale=alt.Scale(type="utc", domain=[ticks[0], ticks[-1]], nice=False),
                 axis=alt.Axis(format="%d.%m.%Y", values=ticks, labelOverlap=True))


def price_scale(values):
    low = math.floor(values.min() / 100) * 100
    high = math.ceil(values.max() / 100) * 100
    return alt.Scale(domain=[low, max(high, low + 100)], nice=False, zero=False)


def show_daily_chart(selected, days, today):
    prices = selected[["date", "price", "is_promo"]].copy()
    prices["date"] = prices["date"].dt.tz_localize("UTC")
    prices["День"] = prices["date"].dt.strftime("%d.%m.%Y")
    prices["Статус"] = prices["is_promo"].map({True: "Акция", False: "Без акции"}).fillna("Неизвестно")
    prices["price_label"] = prices["price"].map(lambda v: metric_amount(v, "₴"))
    prices["segment"] = prices["date"].diff().dt.days.ne(1).cumsum()
    promo = prices.loc[prices["Статус"].eq("Акция")].copy()
    promo["segment"] = promo["date"].diff().dt.days.ne(1).cumsum()

    base = alt.Chart(prices).encode(x=daily_axis(prices["date"], days, today),
                                    y=alt.Y("price:Q", title="грн", scale=price_scale(prices["price"])))
    line = base.mark_line(color="#2563EB").encode(detail="segment:N")
    red = line.properties(data=promo).mark_line(color="#DC2626")
    points = base.mark_circle(size=65).encode(
        color=alt.condition(alt.datum["Статус"] == "Акция", alt.value("#DC2626"), alt.value("#2563EB")),
        tooltip=["День:N", price_tooltip(), "Статус:N"])
    st.altair_chart(style_chart(line + red + points), theme=None, width="stretch")


MATCH_LABELS = {"exact": "Точное", "equivalent": "Сопоставимое", "unmatched": "Без соответствия"}


def load_mapping():
    root = HISTORY.parent.parent
    fields = ["product_id", "canonical_name", *KEY, "unit", "match_type"]
    mapping = pd.read_csv(root / "product_mapping.csv", dtype=str, keep_default_na=False,
                          usecols=fields).apply(lambda col: col.str.strip())
    catalog = pd.read_csv(root / "products.csv", dtype=str, usecols=KEY, keep_default_na=False)
    if (mapping.empty or mapping.eq("").any().any() or mapping.duplicated(KEY).any()
            or not mapping["match_type"].isin(MATCH_LABELS).all()
            or not mapping.set_index(KEY).index.isin(catalog.set_index(KEY).index).all()):
        raise ValueError("Проверьте поля, типы соответствия и пары поставщик/SKU в справочнике.")
    comparable = mapping[mapping["match_type"] != "unmatched"]
    if (mapping.groupby("product_id")["canonical_name"].nunique().gt(1).any()
            or comparable.groupby("product_id")["unit"].nunique().gt(1).any()
            or comparable.duplicated(["product_id", "supplier"]).any()):
        raise ValueError("Неоднозначная группа товаров или разные единицы измерения.")
    return mapping


def freshness(day, today):
    if pd.isna(day):
        return "Нет данных"
    age = (today - day).days
    if age < 0:
        return "Дата в будущем"
    return "Свежая" if age <= 1 else "Устарела"


def show_comparison(data, today, mapping):
    if mapping is None:
        st.info("Сравнение недоступно без справочника товаров.")
        return
    names = mapping.set_index("product_id")["canonical_name"].to_dict()
    kpis = st.container()
    left, right = st.columns([2, 1])
    product_id = left.selectbox("Главный товар", list(names), format_func=names.get,
                                on_change=st.session_state.update, args=({"compare_page": 1},))
    period = right.selectbox("Период сравнения", ["7 дней", "30 дней", "90 дней", "Весь период"],
                             on_change=st.session_state.update, args=({"compare_page": 1},))
    group = mapping.loc[mapping["product_id"].eq(product_id) & mapping["match_type"].ne("unmatched")]
    if group.empty:
        st.info("Для этого товара нет сопоставимых позиций.")
        return
    rows = data.merge(group, on=KEY, validate="many_to_one").sort_values(["supplier", "date"])
    rows = rows[rows["date"].dt.date <= today]
    latest = group.merge(rows.groupby("supplier").tail(1)[[*KEY, "date", "price"]], on=KEY, how="left")
    fresh = latest["date"].dt.date.map(lambda day: freshness(day, today)).eq("Свежая")
    if fresh.any():
        prices = latest.loc[fresh, "price"]
        low, high = prices.min(), prices.max()
        spread = high - low
        with kpis:
            metric_cards("compare", ["Минимум у поставщиков", "Максимум у поставщиков", "Разница, грн", "Разница, %"],
                         [metric_amount(low, "₴"), metric_amount(high, "₴"),
                          metric_amount(spread if len(prices) > 1 else None, "₴"),
                          metric_amount(100 * spread / low if len(prices) > 1 else None, "%")])
    else:
        kpis.info("Нет свежих цен")

    st.subheader("История цен поставщиков")
    days = None if period == "Весь период" else int(period.split()[0])
    if days:
        start = today - timedelta(days=days - 1)
        rows = rows.loc[rows["date"].dt.date >= start]
    if rows.empty:
        st.info("За выбранный период наблюдений нет.")
    else:
        show_supplier_history(rows, group, days, today)

    if (~fresh).any():
        missing = ", ".join(latest.loc[~fresh, "supplier"])
        st.warning(f"Нет свежих наблюдений: {missing}. Эти поставщики исключены из расчёта.")


def show_supplier_history(rows, group, days, today):
    table = rows.pivot(index="date", columns="supplier", values="price").reindex(columns=group["supplier"])
    # Each sequence of consecutive observations gets its own line.
    gaps = rows.groupby("supplier")["date"].diff().dt.days.ne(1)
    rows = rows.assign(segment=gaps.cumsum(), День=rows["date"].dt.strftime("%d.%m.%Y"),
                       price_label=rows["price"].map(lambda v: metric_amount(v, "₴")),
                       date=rows["date"].dt.tz_localize("UTC"))
    suppliers = sorted(rows["supplier"].unique())
    colors, _, _ = supplier_scales(suppliers)
    base = alt.Chart(rows).encode(
        x=daily_axis(rows["date"], days, today), y=alt.Y("price:Q", title=f"грн/{group['unit'].iloc[0]}",
                                          scale=price_scale(rows["price"])),
        color=alt.Color("supplier:N", title="Поставщик", scale=colors), detail="segment:N",
        tooltip=["День:N", alt.Tooltip("supplier:N", title="Поставщик"),
                 price_tooltip()])
    chart = base.mark_line(strokeWidth=2) + base.mark_circle(size=65)
    st.altair_chart(style_chart(chart), theme=None, width="stretch")
    slot = st.container()
    page = page_rows(table.sort_index(ascending=False), 30, "compare_page")
    with slot:
        show_table(page.reset_index(), ["date", *table.columns], extra_prices=table.columns)


def promo_periods(data, today):
    """Build periods from validated daily observations returned by load_history."""
    periods = []
    for _, rows in data.groupby(KEY, sort=False):
        period = None
        for row in rows.itertuples(index=False):
            day = row.date.date()
            if day > today:
                continue
            if pd.isna(row.is_promo):
                if period is not None:
                    period["status"] = "Неизвестно"
            elif row.is_promo:
                if period is None:
                    period = dict(supplier=row.supplier, product=row.product, sku=row.sku,
                                  first=day, ended=None, min_price=row.price, max_discount=None)
                    periods.append(period)
                period["last"] = day
                period["days"] = (day - period["first"]).days + 1
                period["min_price"] = min(period["min_price"], row.price)
                if pd.notna(row.discount_pct):
                    previous = period["max_discount"]
                    period["max_discount"] = row.discount_pct if previous is None else max(previous, row.discount_pct)
                period["status"] = "Активна" if freshness(day, today) == "Свежая" else "Неизвестно"
            elif period is not None:
                period["ended"] = day
                period["status"] = "Завершена"
                period = None
    return pd.DataFrame(periods)


def show_promo_history(data, current, today):
    st.subheader("История промоакций")
    left, middle, right = st.columns([1, 2, 1])
    supplier = left.selectbox("Поставщик", [None, *sorted(current["supplier"].unique())],
                              format_func=lambda value: value or "Все поставщики", key="promo_supplier")
    if supplier is not None:
        current = current[current["supplier"] == supplier]
        data = data[data["supplier"] == supplier]
    names = current.set_index(KEY)["product"].to_dict()
    item = middle.selectbox("Товар", [None, *names], key="promo_item",
                            format_func=lambda k: f"{names[k]} · {k[0]} · {k[1]}" if k else "Все товары")
    if item is not None:
        data = data[data["supplier"].eq(item[0]) & data["sku"].eq(item[1])]
    window = right.selectbox("Период", [7, 30, 90, None], index=1, key="promo_window",
                             format_func=lambda days: f"{days} дней" if days else "Всё время")
    periods = promo_periods(data, today)
    if window and not periods.empty:
        cutoff = today - timedelta(days=window - 1)
        through = periods["ended"].where(periods["ended"].notna(), periods["last"])
        periods = periods.loc[through >= cutoff]
    st.caption("Показаны периоды, пересекающие выбранный интервал по датам наблюдений. "
               "Длительность — от первого до последнего подтверждения включительно. "
               "Активность подтверждается наблюдением за сегодня или вчера по Киеву.")
    values = ["Нет данных"] * 4
    if not periods.empty:
        closed = periods[periods["status"] == "Завершена"]
        values[:2] = [int(periods["status"].eq("Активна").sum()), len(closed)]
        if periods["max_discount"].notna().any():
            values[2] = metric_amount(periods["max_discount"].mean(), "%")
        if not closed.empty:
            values[3] = f"{closed['days'].mean():.1f}".replace(".", ",") + " дн."
    metric_cards("promos", ["Действующих акций", "Завершённых акций",
                 "Средняя максимальная скидка", "Средняя длительность завершённых"], values)
    if periods.empty:
        st.info("Подтверждённых акционных периодов по выбранным фильтрам нет.")
        return
    config = {k: LABELS[k] for k in ["supplier", "product", "sku"]}
    for field, label in [("first", "Первое обнаружение"), ("last", "Последнее подтверждение"),
                         ("ended", "Впервые без акции")]:
        config[field] = st.column_config.DateColumn(label, format="DD.MM.YYYY")
    config.update(days="Дней", min_price=st.column_config.NumberColumn("Мин. цена"),
                  max_discount=st.column_config.NumberColumn("Макс. скидка"), status="Статус")
    order = ["supplier", "product", "status", "first", "last", "ended", "days", "min_price", "max_discount", "sku"]
    styled_table(periods.sort_values("first", ascending=False)[order], config, money=["min_price"], percent=["max_discount"])


def show_diagnostics(current, today, catalog):
    if catalog is None:
        st.warning("Каталог не удалось прочитать. Диагностика позиций недоступна.")
        return False
    positions = catalog.copy()
    try:
        path = HISTORY.with_name("collection_status.csv")
        fields = [*KEY, "status", "checked_at", "reason"]
        attempts = pd.DataFrame(columns=fields)
        if path.exists():
            attempts = pd.read_csv(path, dtype=str, usecols=fields, keep_default_na=False)
            attempts["checked_at"] = pd.to_datetime(attempts["checked_at"], format="ISO8601", utc=True).dt.tz_convert(KYIV)
        else:
            st.info("Файл диагностики ещё не создан. Статусы появятся после сбора.")
        positions = positions.merge(attempts, on=KEY, how="left", validate="one_to_one")
        successes = current.reindex(columns=[*KEY, "price", "date"])
        positions = positions.merge(successes, on=KEY, how="left", validate="one_to_one")
    except (OSError, ValueError) as exc:
        st.warning(f"Не удалось загрузить диагностику: {exc}")
        return False
    positions["date"] = pd.to_datetime(positions["date"])
    positions["Свежесть"] = positions["date"].dt.date.map(lambda day: freshness(day, today))
    positions["status"] = positions["status"].fillna("Нет попытки")
    counts = positions["status"].value_counts()
    stale = positions["Свежесть"].eq("Устарела").sum()
    metric_cards("quality", ["Отслеживается", "Успешно", "Ошибок",
            "Без цены", "Нет в наличии", "Устаревших цен"], [len(positions), counts.get("ok", 0),
            counts.get("error", 0), counts.get("price_missing", 0), counts.get("out_of_stock", 0), stale])
    if stale:
        st.warning(f"Устаревшие цены: {stale}. Последняя успешная проверка была раньше вчерашнего дня.")
    left, right = st.columns(2)
    supplier = left.selectbox("Поставщик", ["Все", *sorted(positions["supplier"].unique())], key="diag_supplier")
    statuses = {"Все": "Все", "ok": "Успешно", "error": "Ошибка", "price_missing": "Без цены",
                "out_of_stock": "Нет в наличии", "Нет попытки": "Нет попытки"}
    status = right.selectbox("Статус последней попытки", list(statuses), format_func=statuses.get, key="diag_status")
    if supplier != "Все":
        positions = positions[positions["supplier"].eq(supplier)]
    if status != "Все":
        positions = positions[positions["status"].eq(status)]
    if positions.empty:
        st.info("По выбранным фильтрам позиций нет.")
        return path.exists()
    positions = positions.copy()
    positions["status"] = positions["status"].replace(statuses)
    positions["reason"] = positions["reason"].replace("", pd.NA)
    positions["date"] = positions["date"].dt.date
    config = dict(supplier="Поставщик", product="Товар", sku="SKU", status="Статус",
                  checked_at=st.column_config.DatetimeColumn("Последняя попытка по Киеву", timezone="Europe/Kyiv",
                                                            format="DD.MM.YYYY HH:mm"),
                  price=st.column_config.NumberColumn("Последняя успешная цена"),
                  date=st.column_config.DateColumn("Последнее успешное наблюдение", format="DD.MM.YYYY"),
                  Свежесть="Свежесть", reason="Причина")
    order = ["product", "supplier", "status", "checked_at", "reason", "price", "date", "Свежесть", "sku"]
    styled_table(positions[order], config, money=["price"])
    return path.exists()


def main():
    st.set_page_config(page_title="SPM", page_icon="📊", layout="wide")
    dashboard_layout()
    with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
        with st.container(horizontal=True, vertical_alignment="center", width="content"):
            st.title("SPM", anchor=False, width="content")
            st.caption("Supplier Price Monitor", width="content")
        refresh = st.button("Обновить отображение", help="Повторно прочитать данные Dashboard")
    feedback = st.empty()
    overview, history, comparison, promos, status = st.tabs(["Обзор", "История", "Сравнение", "Промоакции", "Состояние данных"])
    try:
        catalog = pd.read_csv(HISTORY.parent.parent / "products.csv", dtype=str, usecols=[*KEY, "product", "url"])
    except (OSError, ValueError):
        catalog = None
    try:
        mapping = load_mapping()
    except (OSError, ValueError) as exc:
        mapping = None
        st.warning(f"Справочник товаров недоступен. {exc}")
    try:
        data = load_history()
        error = "В истории пока нет наблюдений." if data.empty else None
    except FileNotFoundError:
        data, error = pd.DataFrame(), "Файл data/price_history.csv пока отсутствует."
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        data, error = pd.DataFrame(), f"Не удалось прочитать историю. {exc}"
    if error:
        if refresh:
            feedback.error(error)
        for tab in [overview, history, comparison, promos]:
            with tab:
                st.info("Успешных наблюдений пока нет." if error == "В истории пока нет наблюдений."
                        else "Данные недоступны. Подробности на вкладке «Состояние данных».")
        if catalog is not None:
            with overview:
                show_current_prices(catalog, data, mapping)
        with status:
            st.warning(error)
            show_diagnostics(pd.DataFrame(), datetime.now(KYIV).date(), catalog)
            st.caption("Источник — локальный CSV приложения. Состояние GitHub Actions здесь не проверяется.")
        return

    current = latest_prices(data)
    last = data["checked_at"].max()
    today = datetime.now(KYIV).date()
    fresh = current["date"].dt.date.map(lambda day: freshness(day, today)).eq("Свежая")
    active = current.loc[current["is_promo"].eq(True).fillna(False) & fresh]
    items = catalog if catalog is not None else current[[*KEY, "product", "url"]]
    with overview:
        metric_cards("overview",
                ["Товаров", "Поставщиков", "Активных акций", "Последнее наблюдение"],
                [len(items), items["supplier"].nunique(), len(active), last.strftime("%d.%m.%Y")])
        show_current_prices(items, current.loc[fresh], mapping)
        if (~fresh).any():
            st.warning("Устаревшие или будущие наблюдения исключены из актуальных цен. Подробности — в «Состоянии данных».")
    with history:
        left, middle, right = st.columns([1, 2, 1])
        supplier = left.selectbox("Поставщик", sorted(data["supplier"].unique()))
        products = current.loc[current["supplier"].eq(supplier)].set_index("sku")["product"].to_dict()
        sku = middle.selectbox("Товар", list(products), format_func=lambda key: f"{products[key]} · {key}")
        period = right.selectbox("Период", ["7 дней", "30 дней", "60 дней", "90 дней", "Весь период"])
        days = None if period == "Весь период" else int(period.split()[0])
        selected = filter_history(data, supplier, sku, days, today)
        if selected.empty:
            st.info("За выбранный период наблюдений нет. Выберите другой период.")
        else:
            if len(selected) < 2:
                st.info("Одно наблюдение за период. Для линии изменения цены нужно хотя бы два.")
            show_daily_chart(selected, days, today)
            show_price_table(table_prices(selected.sort_values(["date", "checked_at"], ascending=False)))
    with comparison:
        show_comparison(data, today, mapping)
    with promos:
        st.subheader("Действующие акции по последним наблюдениям")
        if active.empty:
            st.info("Среди последних наблюдений подтверждённых акций нет.")
        else:
            show_table(active, ["supplier", "product", "sku", "date", "price", "regular_price", "discount_pct"])
        unknown = (current["is_promo"].isna() | ~fresh).sum()
        if unknown:
            st.caption(f"Статус акции неизвестен у {unknown} товаров.")
        show_promo_history(data, current, today)
    with status:
        diagnostics_ok = show_diagnostics(current, today, catalog)
        left, right = st.columns(2)
        left.metric("Последнее наблюдение по Киеву", last.strftime("%d.%m.%Y %H:%M"))
        right.metric("Записей истории", len(data))
        age = (today - last.date()).days
        if age > 1:
            st.warning(f"Последнее наблюдение старше одного календарного дня по Киеву — {age} дн.")
        elif age < 0:
            st.warning("Дата последнего наблюдения находится в будущем. Проверьте исходные данные.")
        else:
            st.info("Последнее наблюдение датировано сегодня или вчера по Киеву.")
        st.caption("Источник — data/price_history.csv. Свежесть наблюдений не подтверждает успешность GitHub Actions или доставки в Google Drive.")

    if refresh:
        if catalog is not None and mapping is not None and diagnostics_ok:
            feedback.success("Данные повторно загружены. Новый сбор цен не запускался.")
        else:
            feedback.error("Не удалось повторно загрузить все данные Dashboard.")


if __name__ == "__main__":
    main()
