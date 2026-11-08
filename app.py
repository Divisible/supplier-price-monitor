"""Read-only price history dashboard. Run with streamlit run app.py."""
import csv
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

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


def show_table(data, columns):
    display = data[columns].copy()
    if "is_promo" in display:
        display["is_promo"] = display["is_promo"].map({True: "Да", False: "Нет"}).fillna("Неизвестно")
    if "date" in display:
        display["date"] = display["date"].dt.strftime("%d.%m.%Y")
    if "checked_at" in display:
        display["checked_at"] = display["checked_at"].dt.strftime("%d.%m.%Y %H:%M:%S")
    config = {}
    for field in ["price", "regular_price", "previous_price", "change"]:
        config[field] = st.column_config.NumberColumn(LABELS[field], format="%.2f ₴")
    for field in ["discount_pct", "change_pct"]:
        config[field] = st.column_config.NumberColumn(LABELS[field], format="%.2f %%")
    config["url"] = st.column_config.LinkColumn("Карточка товара", display_text="Открыть")
    for field in columns:
        config.setdefault(field, LABELS.get(field, field))
    st.dataframe(display, column_config=config, hide_index=True, width="stretch")


def show_current_prices(current):
    columns = ["supplier", "product", "sku", "price", "change", "change_pct",
               "regular_price", "discount_pct", "is_promo"]
    page_size = 20
    page_count = (len(current) + page_size - 1) // page_size
    if page_count > 1:
        sort, direction, pager = st.columns([2, 2, 1])
        field = sort.selectbox("Сортировать по", columns, format_func=LABELS.get)
        order = direction.selectbox("Порядок", ["По возрастанию", "По убыванию"])
        current = current.sort_values(field, ascending=order == "По возрастанию", kind="stable")
        page = pager.selectbox("Страница", range(1, page_count + 1))
        start = (page - 1) * page_size
        current = current.iloc[start:start + page_size]
    show_table(current, columns)


def daily_axis(dates):
    dates = dates.drop_duplicates().sort_values()
    step = max(1, (len(dates) + 7) // 8)
    ticks = [int(day.timestamp()) * 1000 for day in dates.iloc[::step]]
    return alt.X("date:T", title="Дата", scale=alt.Scale(type="utc"),
                 axis=alt.Axis(format="%d.%m.%Y", values=ticks))


def show_daily_chart(selected):
    prices = selected[["date", "price", "regular_price"]].rename(columns={
        "price": "Цена продажи", "regular_price": "Обычная цена"})
    if prices["Обычная цена"].isna().all():
        prices = prices.drop(columns="Обычная цена")
    prices = prices.melt(id_vars="date", var_name="Тип цены", value_name="Цена")
    # Encode Kyiv calendar dates at UTC midnight so the browser cannot shift the day.
    prices["День"] = prices["date"].dt.strftime("%d.%m.%Y")
    prices["date"] = prices["date"].dt.tz_localize("UTC")
    chart = alt.Chart(prices).mark_line(point=True).encode(
        x=daily_axis(prices["date"]),
        y=alt.Y("Цена:Q", title="грн", scale=alt.Scale(zero=False)),
        color="Тип цены:N",
        tooltip=["День:N", "Тип цены:N", alt.Tooltip("Цена:Q", format=".2f")],
    )
    st.altair_chart(chart.properties(height=320), width="stretch")


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


def show_comparison(data, today):
    try:
        mapping = load_mapping()
    except (OSError, ValueError) as exc:
        st.warning(f"Сравнение недоступно. {exc}")
        return
    names = mapping.set_index("product_id")["canonical_name"].to_dict()
    left, right = st.columns([2, 1])
    product_id = left.selectbox("Главный товар", list(names), format_func=names.get)
    period = right.selectbox("Период сравнения", ["7 дней", "30 дней", "90 дней", "Весь период"], index=1)
    group = mapping.loc[mapping["product_id"].eq(product_id) & mapping["match_type"].ne("unmatched")]
    if group.empty:
        st.info("Для этого товара нет сопоставимых позиций.")
        return
    rows = data.merge(group, on=KEY, validate="many_to_one").sort_values(["supplier", "date"])
    rows = rows[rows["date"].dt.date <= today]
    latest = group.merge(rows.groupby("supplier").tail(1)[[*KEY, "date", "price"]], on=KEY, how="left")
    latest["Соответствие"] = latest["match_type"].map(MATCH_LABELS)
    fresh = latest["date"].ge(pd.Timestamp(today - timedelta(days=1)))
    columns = ["supplier", "sku", "price", "date", "Соответствие"]
    st.subheader("Актуальные цены")
    st.caption("Учитываются наблюдения за сегодня и вчера по Киеву. Разница в процентах рассчитана от минимальной цены.")
    if fresh.any():
        prices = latest.loc[fresh, "price"]
        low, high = prices.min(), prices.max()
        metrics = st.columns(4)
        metrics[0].metric("Минимум у поставщиков", f"{low:.2f} ₴")
        metrics[1].metric("Максимум у поставщиков", f"{high:.2f} ₴")
        spread = high - low
        metrics[2].metric("Разница, грн", f"{spread:.2f}" if len(prices) > 1 else "—")
        metrics[3].metric("Разница, %", f"{100 * spread / low:.2f}" if len(prices) > 1 else "—")
        show_table(latest.loc[fresh], columns)
    if (~fresh).any():
        st.warning("У этих поставщиков наблюдение устарело или отсутствует. Их цены исключены из показателей.")
        show_table(latest.loc[~fresh], columns)

    st.subheader("История цен поставщиков")
    if period != "Весь период":
        start = today - timedelta(days=int(period.split()[0]) - 1)
        rows = rows.loc[rows["date"].dt.date >= start]
    if rows.empty:
        st.info("За выбранный период наблюдений нет.")
        return
    table = rows.pivot(index="date", columns="supplier", values="price").reindex(columns=group["supplier"])
    # Each sequence of consecutive observations gets its own line.
    gaps = rows.groupby("supplier")["date"].diff().dt.days.ne(1)
    rows = rows.assign(segment=gaps.cumsum(), День=rows["date"].dt.strftime("%d.%m.%Y"),
                       date=rows["date"].dt.tz_localize("UTC"))
    colors = {"Фора": "#c93438", "АТБ": "#2869b5", "Варус": "#de7418"}
    suppliers = sorted(rows["supplier"].unique())
    scale = (alt.Scale(domain=suppliers, range=[colors[s] for s in suppliers])
             if all(s in colors for s in suppliers) else alt.Scale(scheme="tableau10"))
    chart = alt.Chart(rows).mark_line(point=True).encode(
        x=daily_axis(rows["date"]), y=alt.Y("price:Q", title=f"грн/{group['unit'].iloc[0]}",
                                          scale=alt.Scale(zero=False)),
        color=alt.Color("supplier:N", title="Поставщик", scale=scale), detail="segment:N",
        tooltip=["День:N", alt.Tooltip("supplier:N", title="Поставщик"),
                 alt.Tooltip("price:Q", title="Цена, грн", format=".2f")])
    st.altair_chart(chart.properties(height=320), width="stretch")
    show_table(table.reset_index(), ["date", *table.columns])


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
                period["status"] = "Активна" if (today - day).days <= 1 else "Неизвестно"
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
    metrics = st.columns(4)
    values = ["Нет данных"] * 4
    if not periods.empty:
        closed = periods[periods["status"] == "Завершена"]
        values[:2] = [int(periods["status"].eq("Активна").sum()), len(closed)]
        if periods["max_discount"].notna().any():
            values[2] = f"{periods['max_discount'].mean():.2f} %"
        if not closed.empty:
            values[3] = f"{closed['days'].mean():.1f} дн."
    for metric, label, value in zip(metrics, ["Действующих акций", "Завершённых акций",
            "Средняя максимальная скидка", "Средняя длительность завершённых"], values):
        metric.metric(label, value)
    if periods.empty:
        st.info("Подтверждённых акционных периодов по выбранным фильтрам нет.")
        return
    config = {k: LABELS[k] for k in ["supplier", "product", "sku"]}
    for field, label in [("first", "Первое обнаружение"), ("last", "Последнее подтверждение"),
                         ("ended", "Впервые без акции")]:
        config[field] = st.column_config.DateColumn(label, format="DD.MM.YYYY")
    config.update(days="Дней", min_price=st.column_config.NumberColumn("Мин. цена", format="%.2f ₴"),
                  max_discount=st.column_config.NumberColumn("Макс. скидка", format="%.2f %%"), status="Статус")
    st.dataframe(periods.sort_values("first", ascending=False), column_order=list(config),
                 column_config=config, hide_index=True, width="stretch")


def main():
    st.set_page_config(page_title="SPM", page_icon="📊", layout="wide")
    st.title("SPM")
    st.caption("Цены поставщиков · история наблюдений · промоакции")
    st.button("Обновить данные", help="Повторно прочитать доступный приложению CSV.")
    overview, history, comparison, promos, status = st.tabs(["Обзор", "История", "Сравнение", "Промоакции", "Состояние данных"])
    try:
        data = load_history()
        error = "В истории пока нет наблюдений." if data.empty else None
    except FileNotFoundError:
        data, error = pd.DataFrame(), "Файл data/price_history.csv пока отсутствует."
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        data, error = pd.DataFrame(), f"Не удалось прочитать историю. {exc}"
    if error:
        for tab in [overview, history, comparison, promos]:
            with tab:
                st.info("Данные недоступны. Подробности на вкладке «Состояние данных».")
        with status:
            st.warning(error)
            st.caption("Источник — локальный CSV приложения. Состояние GitHub Actions здесь не проверяется.")
        return

    current = latest_prices(data)
    last = data["checked_at"].max()
    today = datetime.now(KYIV).date()
    active = current.loc[current["is_promo"].eq(True).fillna(False)]
    with overview:
        for column, label, value in zip(st.columns(4),
                ["Товаров", "Поставщиков", "Активных акций", "Последнее наблюдение"],
                [len(current), current["supplier"].nunique(), len(active), last.strftime("%d.%m.%Y")]):
            column.metric(label, value)
        st.subheader("Актуальные цены")
        st.caption("Последнее наблюдение каждого товара. Изменение — относительно предыдущего доступного наблюдения.")
        show_current_prices(current)
        if current["previous_price"].isna().any():
            st.info("Для части товаров пока есть только одно наблюдение. Изменение цены появится после следующего.")
    with history:
        left, middle, right = st.columns([1, 2, 1])
        supplier = left.selectbox("Поставщик", sorted(data["supplier"].unique()))
        products = current.loc[current["supplier"].eq(supplier)].set_index("sku")["product"].to_dict()
        sku = middle.selectbox("Товар", list(products), format_func=lambda key: f"{products[key]} · {key}")
        period = right.selectbox("Период", ["7 дней", "30 дней", "60 дней", "90 дней", "Весь период"], index=1)
        days = None if period == "Весь период" else int(period.split()[0])
        selected = filter_history(data, supplier, sku, days, today)
        if selected.empty:
            st.info("За выбранный период наблюдений нет. Выберите другой период.")
        else:
            if len(selected) < 2:
                st.info("Одно наблюдение за период. Для линии изменения цены нужно хотя бы два.")
            show_daily_chart(selected)
            show_table(selected, ["date", "checked_at", "price", "regular_price", "discount_pct", "is_promo", "url"])
    with comparison:
        show_comparison(data, today)
    with promos:
        st.subheader("Действующие акции по последним наблюдениям")
        fresh = current["date"].dt.date.between(today - timedelta(days=1), today)
        fresh_active = active.loc[fresh.reindex(active.index)]
        if fresh_active.empty:
            st.info("Среди последних наблюдений подтверждённых акций нет.")
        else:
            show_table(fresh_active, ["supplier", "product", "sku", "date", "price", "regular_price", "discount_pct"])
        unknown = (current["is_promo"].isna() | ~fresh).sum()
        if unknown:
            st.caption(f"Статус акции неизвестен у {unknown} товаров.")
        show_promo_history(data, current, today)
    with status:
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


if __name__ == "__main__":
    main()
