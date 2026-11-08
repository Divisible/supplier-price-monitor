"""Shared charts, tables and layout for the SPM dashboard."""
import altair as alt
import pandas as pd
import streamlit as st


# Color, dash pattern and marker are stable across filters.
SUPPLIER_STYLES = {
    "Фора": ("#1D4ED8", [1, 0], "circle"),
    "АТБ": ("#0F7676", [8, 4], "square"),
    "Варус": ("#7C3AED", [3, 3], "triangle-up"),
}


def supplier_scales(suppliers):
    domain = list(suppliers)
    styles = [SUPPLIER_STYLES.get(name, ("#475569", [8, 3, 2, 3], "diamond")) for name in domain]
    return tuple(alt.Scale(domain=domain, range=list(values)) for values in zip(*styles))


def price_tooltip():
    return alt.Tooltip("price_label:N", title="Цена")


def metric_amount(value, unit, signed=False):
    if pd.isna(value):
        return "—"
    number = f"{value:.2f}".replace(".", ",")
    if signed and value > 0:
        number = "+" + number
    number = number.replace("-", "−")
    return f"{number}%" if unit == "%" else f"{number} {unit}"


def style_chart(chart):
    return (chart.properties(height=320, background="#FFFFFF")
            .configure_axisY(labelExpr="replace(datum.label, '.', ',')")
            .configure(font="sans-serif")
            .configure_view(stroke=None)
            .configure_axis(labelColor="#64748B", titleColor="#172B4D", labelFontSize=12,
                            titleFontSize=13, titlePadding=10, labelPadding=5,
                            gridColor="#E2E8F0", domainColor="#64748B", tickColor="#64748B")
            .configure_legend(orient="top", direction="horizontal", labelColor="#172B4D",
                              titleColor="#172B4D", labelFontSize=12, symbolSize=80))


def metric_cards(key, labels, values):
    with st.container(key=f"cards_{key}"):
        for label, value in zip(labels, values):
            st.metric(label, value, border=True)


def dashboard_layout():
    # Scope responsive grid rules to containers explicitly named by this app.
    st.html("""<style>
    :is(.st-key-cards_overview,.st-key-cards_compare,.st-key-cards_promos,.st-key-cards_quality) {
        display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.75rem;
    }
    .st-key-cards_quality {grid-template-columns:repeat(3,minmax(0,1fr))}
    :is(.st-key-cards_overview,.st-key-cards_compare,.st-key-cards_promos,.st-key-cards_quality) > div {
        min-width:0; background:#fff; border-radius:.375rem;
    }
    @media(max-width:700px) {
        :is(.st-key-cards_overview,.st-key-cards_compare,.st-key-cards_promos,.st-key-cards_quality) {
            grid-template-columns:repeat(2,minmax(0,1fr)); gap:.5rem;
        }
        :is(.st-key-cards_overview,.st-key-cards_compare,.st-key-cards_promos,.st-key-cards_quality) label p {
            white-space:normal; overflow:visible;
        }
    }
    </style>""")


STATUS_COLORS = {"Активна": "#15803D", "Завершена": "#475569", "Неизвестно": "#B45309",
                 "Успешно": "#15803D", "Ошибка": "#B91C1C", "Без цены": "#B45309",
                 "Нет в наличии": "#B45309", "Свежая": "#15803D", "Устарела": "#B45309",
                 "Минимум": "#15803D"}


def change_color(value):
    if pd.isna(value) or value == 0:
        return "color: #64748B"
    return "color: #B91C1C" if value > 0 else "color: #15803D"


def styled_table(data, config, *, money=(), percent=()):
    # Styler supplies display strings; Arrow still carries numeric values for sorting.
    formats = {}
    for field in money:
        formats[field] = lambda v: metric_amount(v, "₴")
    for field in percent:
        formats[field] = lambda v: metric_amount(v, "%")
    for field, unit in [("change", "₴"), ("change_pct", "%")]:
        if field in data:
            formats[field] = lambda v, u=unit: metric_amount(v, u, signed=True)
    style = data.style.format(formats, na_rep="—")
    for field in ("change", "change_pct"):
        if field in data:
            style = style.map(change_color, subset=[field])
    for field in ("status", "Свежесть", "Цена среди свежих"):
        if field in data:
            style = style.map(lambda v: f"color: {STATUS_COLORS.get(v, '#64748B')}", subset=[field])
    if "discount_pct" in data:
        style = style.map(lambda v: "color: #15803D" if pd.notna(v) and v > 0 else "",
                          subset=["discount_pct"])
    st.dataframe(style, column_config=config, column_order=list(data.columns), placeholder="—",
                 hide_index=True, width="stretch")
