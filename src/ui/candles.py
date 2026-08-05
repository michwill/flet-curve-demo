"""A candlestick chart, drawn by hand on a Flet canvas.

Flet 0.86 ships no chart controls in core at all -- and no charting package
in the ecosystem draws candlesticks anyway -- so this renders the bars
directly with `flet.canvas` primitives (Rect, Line, Text). That turns out to
be the right level: a candle is two rectangles and a line, and drawing them
ourselves is what makes the price-line, the axis labels and the theme
colours behave.

The layout maths is deliberately kept in `plot_geometry`, which is a pure
function of numbers and therefore testable without a canvas.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import flet as ft
import flet.canvas as cv

from curve.api import Candle
from curve.format import token_amount

from . import safe_update

# A rising candle is green and a falling one red, which is conventional
# enough that inverting it would be actively confusing. These are picked to
# clear 3:1 contrast on both the light and dark surfaces -- Material 3 has
# no semantic role for "up"/"down", so fixed shades are the honest choice.
UP = ft.Colors.GREEN_600
DOWN = ft.Colors.RED_400

#: Room for the price axis on the right, in pixels.
AXIS_WIDTH = 62.0
#: Room for the date axis along the bottom.
AXIS_HEIGHT = 22.0
PADDING = 10.0


@dataclass(slots=True, frozen=True)
class Geometry:
    """Where the candles go, given a canvas size and a price range."""

    low: float
    high: float
    plot_width: float
    plot_height: float
    candle_width: float
    step: float

    def y(self, price: float) -> float:
        """Price -> pixels from the top of the plot area."""
        if self.high <= self.low:
            return self.plot_height / 2
        ratio = (price - self.low) / (self.high - self.low)
        return PADDING + (1.0 - ratio) * self.plot_height

    def x(self, index: int) -> float:
        """Candle index -> the pixel centre of its slot."""
        return PADDING + index * self.step + self.step / 2


def plot_geometry(candles: list[Candle], width: float, height: float) -> Geometry:
    """Work out the scales for a set of candles in a given box.

    The price range is padded by 4% so the extremes are not drawn flush
    against the frame, and a flat series (every price identical, which
    happens on pegged stable pairs) gets an artificial range rather than
    dividing by zero.
    """
    plot_width = max(width - AXIS_WIDTH - 2 * PADDING, 1.0)
    plot_height = max(height - AXIS_HEIGHT - 2 * PADDING, 1.0)

    if not candles:
        return Geometry(0.0, 1.0, plot_width, plot_height, 1.0, 1.0)

    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    if high <= low:
        # Perfectly flat: invent a range so the line lands mid-box.
        spread = abs(high) * 0.001 or 0.001
        low, high = low - spread, high + spread
    else:
        margin = (high - low) * 0.04
        low, high = low - margin, high + margin

    step = plot_width / len(candles)
    # Leave a hairline gap between candles, but never go sub-pixel.
    candle_width = max(step * 0.7, 1.0)
    return Geometry(low, high, plot_width, plot_height, candle_width, step)


def _axis_prices(geometry: Geometry, count: int = 5) -> list[float]:
    if geometry.high <= geometry.low:
        return [geometry.low]
    stride = (geometry.high - geometry.low) / (count - 1)
    return [geometry.low + i * stride for i in range(count)]


def _price_decimals(span: float) -> int:
    """How many decimals it takes to tell two gridlines apart.

    Choosing by magnitude alone is not enough: a stable pool ranging over
    1.0268-1.0271 needs four decimals to separate its labels even though
    the values are order-1, and rounding to a fixed 4 prints "1.027" three
    times down the axis. Derive it from the *span* instead.
    """
    if span <= 0:
        return 2
    # One digit finer than the gap between labels.
    return max(2, min(10, int(math.ceil(-math.log10(span))) + 2))


def _format_price(value: float, decimals: int = 4) -> str:
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000 and decimals <= 2:
        return f"{value:,.0f}"
    return f"{value:.{decimals}f}"


def _format_date(timestamp: int) -> str:
    return time.strftime("%d %b", time.gmtime(timestamp))


def build_shapes(
    candles: list[Candle],
    width: float,
    height: float,
    *,
    grid_color: str,
    text_color: str,
) -> list[cv.Shape]:
    """Every shape for one chart: gridlines, axes, candles, last-price line."""
    geometry = plot_geometry(candles, width, height)
    shapes: list[cv.Shape] = []

    grid_paint = ft.Paint(color=grid_color, stroke_width=1)
    label_style = ft.TextStyle(size=10, color=text_color)

    # -- horizontal gridlines and the price axis --
    plot_right = PADDING + geometry.plot_width
    decimals = _price_decimals(geometry.high - geometry.low)
    for price in _axis_prices(geometry):
        y = geometry.y(price)
        shapes.append(cv.Line(PADDING, y, plot_right, y, paint=grid_paint))
        shapes.append(
            cv.Text(
                plot_right + 6,
                y - 7,
                _format_price(price, decimals),
                style=label_style,
            )
        )

    if not candles:
        return shapes

    # -- date labels: about one per 90px, so they never collide --
    label_every = max(1, int(len(candles) / max(geometry.plot_width / 90, 1)))
    baseline = PADDING + geometry.plot_height
    for index, candle in enumerate(candles):
        if index % label_every == 0:
            shapes.append(
                cv.Text(
                    geometry.x(index) - 14,
                    baseline + 5,
                    _format_date(candle.time),
                    style=label_style,
                )
            )

    # -- the candles --
    for index, candle in enumerate(candles):
        colour = UP if candle.rising else DOWN
        paint = ft.Paint(color=colour, stroke_width=1)
        centre = geometry.x(index)

        # wick first, so the body draws over it
        shapes.append(
            cv.Line(centre, geometry.y(candle.high), centre, geometry.y(candle.low), paint=paint)
        )

        top = geometry.y(max(candle.open, candle.close))
        bottom = geometry.y(min(candle.open, candle.close))
        # A doji would otherwise be invisible: floor the body at 1px.
        body_height = max(bottom - top, 1.0)
        shapes.append(
            cv.Rect(
                centre - geometry.candle_width / 2,
                top,
                geometry.candle_width,
                body_height,
                paint=ft.Paint(color=colour, style=ft.PaintingStyle.FILL),
            )
        )

    # -- last price, marked across the whole plot --
    last = candles[-1]
    last_colour = UP if last.rising else DOWN
    y = geometry.y(last.close)
    shapes.append(
        cv.Line(
            PADDING,
            y,
            plot_right,
            y,
            paint=ft.Paint(
                color=last_colour,
                stroke_width=1,
                stroke_dash_pattern=[4, 3],
            ),
        )
    )
    shapes.append(
        cv.Text(
            plot_right + 6,
            y - 7,
            _format_price(last.close, decimals),
            style=ft.TextStyle(size=10, color=last_colour, weight=ft.FontWeight.BOLD),
        )
    )
    return shapes


class CandleChart(ft.Container):
    """A canvas that redraws its candles whenever it is resized.

    Flet gives the canvas its size only at layout time, so the shapes are
    built in `on_resize` rather than up front -- the same reason the geometry
    is a pure function taking width and height.
    """

    def __init__(self, height: float = 320) -> None:
        self._candles: list[Candle] = []
        self._width = 800.0
        self._height = height
        self._canvas = cv.Canvas(shapes=[], expand=True, on_resize=self._resized)
        self._empty = ft.Text(
            "No price history for this pair.",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self._stack = ft.Stack(
            controls=[self._canvas, ft.Container(self._empty, alignment=ft.Alignment.CENTER)],
            expand=True,
        )
        super().__init__(
            content=self._stack,
            height=height,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=4,
        )

    def _resized(self, e: cv.CanvasResizeEvent) -> None:
        self._width, self._height = e.width, e.height
        self._redraw()

    def set_candles(self, candles: list[Candle]) -> None:
        self._candles = candles or []
        self._redraw()

    def _redraw(self) -> None:
        self._empty.visible = not self._candles
        self._canvas.shapes = build_shapes(
            self._candles,
            self._width,
            self._height,
            grid_color=ft.Colors.OUTLINE_VARIANT,
            text_color=ft.Colors.ON_SURFACE_VARIANT,
        )
        safe_update(self)

    @property
    def summary(self) -> str:
        """A one-line change-over-window caption for the header."""
        if len(self._candles) < 2:
            return ""
        first, last = self._candles[0].open, self._candles[-1].close
        if not first:
            return ""
        change = (last - first) / first * 100
        arrow = "+" if change >= 0 else "-"
        return f"{token_amount(last)}   {arrow}{abs(change):.2f}%"
