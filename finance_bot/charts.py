"""Render analytics charts for Telegram messages."""

from __future__ import annotations

from decimal import Decimal
from io import BytesIO
from typing import Sequence

from finance_bot.analytics import AnalyticsValue


def render_donut_chart(title: str, values: Sequence[AnalyticsValue]) -> BytesIO:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    positive_values = sorted(
        (value for value in values if value.amount > 0),
        key=lambda value: value.amount,
        reverse=True,
    )
    figure = Figure(figsize=(12, 7), dpi=120, facecolor="white")
    axis = figure.add_subplot(1, 1, 1)
    axis.set_title(title, fontsize=15, pad=16)

    if positive_values:
        axis.set_position([0.02, 0.08, 0.48, 0.80])
        amounts = [float(value.amount) for value in positive_values]
        wedges, _ = axis.pie(
            amounts,
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.42, "edgecolor": "white"},
        )
        total = sum(value.amount for value in positive_values)
        legend = [
            f"{value.label}: {_format_amount(value.amount)} ₽ "
            f"({_format_percent(value.amount / total)})"
            for value in positive_values
        ]
        figure.legend(
            wedges,
            legend,
            loc="upper left",
            bbox_to_anchor=(0.55, 0.92),
            frameon=False,
            fontsize=10,
            labelspacing=0.9,
        )
        axis.set_aspect("equal")
    else:
        axis.text(0, 0, "Нет данных", ha="center", va="center", fontsize=14)
        axis.set_xticks([])
        axis.set_yticks([])

    image = BytesIO()
    image.name = "analytics.png"
    FigureCanvasAgg(figure).print_png(image)
    image.seek(0)
    figure.clear()
    return image


def _format_amount(amount: Decimal) -> str:
    return f"{amount:,.2f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")


def _format_percent(value: Decimal) -> str:
    return f"{value:.1%}".replace(".", ",")
