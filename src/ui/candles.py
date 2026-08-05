"""The pool price chart, built on `flet-charts`' `CandlestickChart`.

An earlier version of this file drew the candles by hand on `flet.canvas`,
on the belief that nothing in the ecosystem drew candlesticks. That was
wrong: `flet-charts` is an official Flet package, released on the same
version line as flet itself, and it is pure Python -- the Dart side ships
with the standard client, so `flet publish` still needs no Flutter build.

Using it is not merely less code. The hand-drawn version was a *picture*:
it could not be hovered, had no tooltips and no transitions, and every axis
label was a `cv.Text` positioned by arithmetic. `CandlestickChart` is a
real control -- interactive, with built-in tooltips, animated updates, and
axes that lay themselves out.

What stays here is the part that is genuinely this app's problem: what
range to show, how many decimals an axis label needs, and how to phrase a
tooltip.
"""

from __future__ import annotations

import math
import time

import flet as ft
import flet_charts as fc

from curve.api import Candle
from curve.format import token_amount

from . import safe_update

#: Roughly how many labels to put on each axis.
PRICE_LABELS = 5
DATE_LABELS = 6


def price_range(candles: list[Candle]) -> tuple[float, float]:
    """The y-axis bounds for a series, padded so nothing sits on the frame.

    A flat series -- every price identical, which pegged stable pairs
    really do produce -- gets an invented range rather than a zero-height
    axis the chart cannot scale.
    """
    if not candles:
        return 0.0, 1.0
    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    if high <= low:
        spread = abs(high) * 0.001 or 0.001
        return low - spread, high + spread
    margin = (high - low) * 0.04
    return low - margin, high + margin


def price_decimals(span: float) -> int:
    """How many decimals a value needs at a given tick interval.

    Magnitude alone is not enough: a stable pool ranging over 1.0268-1.0271
    needs four decimals even though the values are order-1, and rounding to
    a fixed 4 prints "1.027" three times down the axis.
    """
    if span <= 0:
        return 2
    return max(2, min(10, int(math.ceil(-math.log10(span))) + 2))


def interval_decimals(interval: float) -> int:
    """Exactly the decimals needed to write `interval` without padding.

    An axis stepping by 0.0001 wants four (1.0268); one stepping by 250
    wants none. Deriving this from the span instead over-pads, printing
    "1.026800" where "1.0268" is the number.
    """
    if interval <= 0:
        return 2
    for decimals in range(11):
        scaled = interval * 10**decimals
        if abs(scaled - round(scaled)) < 1e-9:
            return decimals
    return 10


def format_price(value: float, decimals: int = 4) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1000 and decimals <= 2:
        return f"{value:,.0f}"
    return f"{value:.{decimals}f}"


def format_date(timestamp: int) -> str:
    return time.strftime("%d %b", time.gmtime(timestamp))


def _axis_label(text: str) -> ft.Control:
    return ft.Text(text, size=10, color=ft.Colors.ON_SURFACE_VARIANT)


def nice_interval(span: float, target: int) -> float:
    """A round tick interval covering `span` in about `target` steps.

    Ticks land on 1, 2, 2.5 or 5 times a power of ten -- the intervals that
    produce readable labels -- rather than on span/N, which gives values
    like 0.003524.
    """
    if span <= 0 or target <= 0:
        return 1.0
    raw = span / target
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= step * magnitude:
            return step * magnitude
    return 10.0 * magnitude


def price_axis(candles: list[Candle]) -> fc.ChartAxis:
    """Price labels along the left edge.

    Labels sit on **multiples of the interval**, not at `min_y + i*step`.
    The chart draws its ticks at multiples of `label_spacing` counted from
    zero, and only renders a label whose value matches a tick -- so labels
    placed at an arbitrary offset from the axis minimum silently vanish,
    leaving just the min and max. The date axis never hit this because its
    values are integer multiples of the stride already.
    """
    low, high = price_range(candles)
    interval = nice_interval(high - low, PRICE_LABELS - 1)
    decimals = interval_decimals(interval)

    values: list[float] = []
    tick = math.ceil(low / interval) * interval
    while tick <= high and len(values) < PRICE_LABELS + 2:
        values.append(tick)
        tick += interval

    return fc.ChartAxis(
        label_size=64,
        label_spacing=interval,
        labels=[
            fc.ChartAxisLabel(value=v, label=_axis_label(format_price(v, decimals)))
            for v in values
        ],
    )


def date_axis(candles: list[Candle]) -> fc.ChartAxis:
    """Dates along the bottom, thinned so they never collide.

    Spots are indexed 0..n-1 rather than by timestamp, so the axis has no
    gaps for hours a pool did not trade and the labels stay ours to place.
    """
    if not candles:
        return fc.ChartAxis(labels=[])
    stride = max(1, len(candles) // DATE_LABELS)
    return fc.ChartAxis(
        label_size=28,
        label_spacing=stride,
        labels=[
            fc.ChartAxisLabel(value=i, label=_axis_label(format_date(candles[i].time)))
            for i in range(0, len(candles), stride)
        ],
    )


def spot_tooltip(candle: Candle, decimals: int) -> str:
    """One candle, spelled out. The chart shows this on hover."""
    return (
        f"{format_date(candle.time)}\n"
        f"O {format_price(candle.open, decimals)}   "
        f"H {format_price(candle.high, decimals)}\n"
        f"L {format_price(candle.low, decimals)}   "
        f"C {format_price(candle.close, decimals)}"
    )


def build_spots(candles: list[Candle]) -> list[fc.CandlestickChartSpot]:
    low, high = price_range(candles)
    decimals = price_decimals(high - low)
    return [
        fc.CandlestickChartSpot(
            x=index,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            tooltip=spot_tooltip(candle, decimals),
        )
        for index, candle in enumerate(candles)
    ]


class CandleChart(ft.Container):
    """A live candlestick chart. Swap its data with `set_candles`."""

    def __init__(self, height: float = 340) -> None:
        self._candles: list[Candle] = []
        self._chart = fc.CandlestickChart(
            key="price-chart",
            spots=[],
            expand=True,
            visible=False,
            interactive=True,
            # Transitions between timeframes rather than a hard cut -- the
            # whole reason for a real chart control over a repainted canvas.
            animation=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
            tooltip=fc.CandlestickChartTooltip(
                bgcolor=ft.Colors.INVERSE_SURFACE,
                border_radius=6,
                fit_inside_horizontally=True,
                fit_inside_vertically=True,
            ),
            horizontal_grid_lines=fc.ChartGridLines(
                color=ft.Colors.OUTLINE_VARIANT, width=1
            ),
        )
        self._empty = ft.Text(
            "No price history for this pair.",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        super().__init__(
            content=ft.Stack(
                [
                    self._chart,
                    ft.Container(self._empty, alignment=ft.Alignment.CENTER),
                ],
                expand=True,
            ),
            height=height,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=8,
        )

    def set_candles(self, candles: list[Candle]) -> None:
        self._candles = candles or []
        self._empty.visible = not self._candles
        self._chart.visible = bool(self._candles)
        self._chart.spots = build_spots(self._candles)
        if self._candles:
            low, high = price_range(self._candles)
            self._chart.min_y, self._chart.max_y = low, high
            self._chart.left_axis = price_axis(self._candles)
            self._chart.bottom_axis = date_axis(self._candles)
        safe_update(self)

    @property
    def summary(self) -> str:
        """A change-over-window caption for the header."""
        if len(self._candles) < 2:
            return ""
        first, last = self._candles[0].open, self._candles[-1].close
        if not first:
            return ""
        change = (last - first) / first * 100
        arrow = "+" if change >= 0 else "-"
        return f"{token_amount(last)}   {arrow}{abs(change):.2f}%"
