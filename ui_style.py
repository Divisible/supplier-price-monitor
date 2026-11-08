"""Shared presentation settings for the SPM dashboard."""
import altair as alt


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


def price_tooltip(field):
    return alt.Tooltip(field, title="Цена, грн", format=".2f")


def metric_amount(value, unit):
    if value is None:
        return "—"
    number = f"{value:.2f}".replace(".", ",")
    return f"{number}%" if unit == "%" else f"{number} {unit}"


def style_chart(chart):
    return (chart.properties(height=320, background="#FFFFFF")
            .configure(font="sans-serif")
            .configure_view(stroke=None)
            .configure_axis(labelColor="#64748B", titleColor="#172B4D", labelFontSize=12,
                            titleFontSize=13, titlePadding=10, labelPadding=5,
                            gridColor="#E2E8F0", domainColor="#64748B", tickColor="#64748B")
            .configure_legend(orient="top", direction="horizontal", labelColor="#172B4D",
                              titleColor="#172B4D", labelFontSize=12, symbolSize=80))
