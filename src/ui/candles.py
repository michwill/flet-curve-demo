"""The pool price chart: `flet-charts`' `CandlestickChart`, made navigable.

An earlier version drew the candles by hand on `flet.canvas`, on the belief
that nothing in the ecosystem drew candlesticks. That was wrong --
`flet-charts` is an official Flet package on the same version line, and it
is pure Python, so the Dart side ships with the standard client and
`flet publish` still needs no Flutter build.

The control draws candles and axes but has no pan, no zoom and no
crosshair, so this module adds them the way the pyqtgraph dashboard this is
modelled on does:

    GestureDetector        drag to pan, wheel to zoom, hover for the crosshair
      Stack
        CandlestickChart   the candles and axes
        Canvas             a transparent overlay -- crosshair only

That split is the point. The chart never repaints for a mouse move; only
the overlay does. And the overlay is a canvas because a crosshair *is* two
lines and a label, which is exactly what canvas is for -- unlike the
candles, which were not.

The arithmetic lives in `viewport.py` so it can be tested on its own.
"""

from __future__ import annotations

import math
import time

import flet as ft
import flet.canvas as cv
import flet_charts as fc

from curve.api import Candle
from curve.format import token_amount

from . import safe_update
from .viewport import MIN_VISIBLE, ZOOM_STEP, Plot, Viewport

#: Roughly how many labels to put on each axis.
PRICE_LABELS = 5
DATE_LABELS = 6

#: Hover fires per mouse move, and each one is a round trip to Python. The
#: Qt version throttles for the same reason; without it a fast sweep across
#: the chart queues hundreds of redraws.
HOVER_INTERVAL = 0.04

#: Candles kept either side of the visible window. Only the window is sent
#: to the chart -- see `build_spots` -- and the margin stops a fast drag
#: showing empty space before the next update lands.
SPOT_MARGIN = 8


def price_decimals(span: float) -> int:
    """How many decimals a value needs across a given span."""
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


def format_datetime(timestamp: int) -> str:
    return time.strftime("%d %b %H:%M", time.gmtime(timestamp))


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


def fit(candles: list[Candle]) -> Viewport:
    """The window showing the whole series, with the usual 4% headroom.

    A flat series -- every price identical, which pegged stable pairs
    really do produce -- gets an invented range rather than a zero-height
    axis the chart cannot scale.
    """
    if not candles:
        return Viewport(0.0, 1.0, 0.0, 1.0)
    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    if high <= low:
        spread = abs(high) * 0.001 or 0.001
        low, high = low - spread, high + spread
    else:
        margin = (high - low) * 0.04
        low, high = low - margin, high + margin
    return Viewport(-0.5, max(len(candles) - 0.5, 0.5), low, high)


def _axis_label(text: str) -> ft.Control:
    return ft.Text(text, size=10, color=ft.Colors.ON_SURFACE_VARIANT)


def price_axis(view: Viewport) -> fc.ChartAxis:
    """Price labels along the left edge.

    Labels sit on **multiples of the interval**, not at `y_min + i*step`.
    The chart draws its ticks at multiples of `label_spacing` counted from
    zero and only renders a label whose value matches a tick -- so labels
    placed at an arbitrary offset silently vanish, leaving just the min and
    max. The date axis never hit this: its values are integer multiples of
    the stride already.
    """
    interval = nice_interval(view.y_span, PRICE_LABELS - 1)
    decimals = interval_decimals(interval)
    values: list[float] = []
    tick = math.ceil(view.y_min / interval) * interval
    while tick <= view.y_max and len(values) < PRICE_LABELS + 2:
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


def date_axis(candles: list[Candle], view: Viewport) -> fc.ChartAxis:
    """Dates along the bottom, thinned to whatever the window shows.

    Recomputed per viewport rather than per series: zoomed into a day, the
    labels should be that day's, not the whole quarter's.
    """
    if not candles:
        return fc.ChartAxis(labels=[])
    stride = max(1, int(view.x_span) // DATE_LABELS)
    first = max(0, int(math.floor(view.x_min)))
    last = min(len(candles) - 1, int(math.ceil(view.x_max)))
    values = [i for i in range(0, len(candles), stride) if first <= i <= last]
    return fc.ChartAxis(
        label_size=28,
        label_spacing=stride,
        labels=[
            fc.ChartAxisLabel(value=i, label=_axis_label(format_date(candles[i].time)))
            for i in values
        ],
    )


def visible_range(candles: list[Candle], view: Viewport) -> tuple[int, int]:
    """The slice of candles worth sending for a given window."""
    if not candles:
        return 0, 0
    start = max(0, int(math.floor(view.x_min)) - SPOT_MARGIN)
    end = min(len(candles), int(math.ceil(view.x_max)) + SPOT_MARGIN + 1)
    return start, max(start, end)


def build_spots(
    candles: list[Candle], view: Viewport | None = None
) -> list[fc.CandlestickChartSpot]:
    """Spots for the visible window, keyed by index into the full series.

    Only the window is sent because at 1Y there is no point serialising 365
    spots on every frame of a drag -- a zoomed-in view sends ~20.

    It does **not** make the candles wider. `CandlestickChart` draws them at
    a fixed pixel width: there is no width property on the chart or on
    `CandlestickChartSpot`, and measuring the rendered result shows the
    width unchanged whether it is handed 365 spots over a 90-day window or
    20 over a 17-day one. Zooming in therefore spreads the candles apart
    rather than fattening them. Getting proper width scaling would mean
    painting the bodies onto the overlay canvas and leaving the control to
    draw only the axes and grid.

    `x` stays the index into the *full* series, so the viewport, the axis
    labels and the crosshair all keep speaking one coordinate system.
    """
    if view is None:
        start, end = 0, len(candles)
    else:
        start, end = visible_range(candles, view)
    return [
        fc.CandlestickChartSpot(
            x=index,
            open=candles[index].open,
            high=candles[index].high,
            low=candles[index].low,
            close=candles[index].close,
            # The crosshair readout replaces the control's own tooltip.
            show_tooltip=False,
        )
        for index in range(start, end)
    ]


def crosshair_shapes(
    plot: Plot,
    view: Viewport,
    candles: list[Candle],
    px: float,
    py: float,
    *,
    line_color: str,
    text_color: str,
    box_color: str,
) -> list[cv.Shape]:
    """Two dashed lines through the cursor, plus a price/time readout."""
    if not plot.contains(px, py):
        return []

    decimals = price_decimals(view.y_span)
    price = plot.data_y(py, view)
    paint = ft.Paint(color=line_color, stroke_width=1, stroke_dash_pattern=[4, 3])
    shapes: list[cv.Shape] = [
        cv.Line(px, plot.top, px, plot.bottom, paint=paint),
        cv.Line(plot.left, py, plot.right, py, paint=paint),
    ]

    # Price, pinned to the left axis so it reads against the scale.
    shapes.append(
        cv.Rect(0, py - 8, plot.left - 2, 16, paint=ft.Paint(color=box_color))
    )
    shapes.append(
        cv.Text(
            4,
            py - 7,
            format_price(price, decimals),
            style=ft.TextStyle(size=10, color=text_color, weight=ft.FontWeight.BOLD),
        )
    )

    index = int(round(plot.data_x(px, view)))
    if 0 <= index < len(candles):
        candle = candles[index]
        # Time, pinned under the cursor on the date axis.
        shapes.append(
            cv.Rect(px - 44, plot.bottom + 2, 88, 15, paint=ft.Paint(color=box_color))
        )
        shapes.append(
            cv.Text(
                px - 40,
                plot.bottom + 3,
                format_datetime(candle.time),
                style=ft.TextStyle(size=10, color=text_color),
            )
        )
        # OHLC for the candle under the cursor, in the top-left corner --
        # out of the way rather than following the pointer around.
        shapes.append(
            cv.Text(
                plot.left + 8,
                6,
                f"O {format_price(candle.open, decimals)}   "
                f"H {format_price(candle.high, decimals)}   "
                f"L {format_price(candle.low, decimals)}   "
                f"C {format_price(candle.close, decimals)}",
                style=ft.TextStyle(size=10, color=text_color),
            )
        )
    return shapes


class CandleChart(ft.Container):
    """A candlestick chart you can drag, zoom and read off.

    Drag pans in both axes, the wheel zooms in time about the cursor,
    hovering shows a crosshair with the price and time under it, and a
    double-tap refits the whole series.
    """

    def __init__(self, height: float = 340) -> None:
        self._candles: list[Candle] = []
        self._view = Viewport(0.0, 1.0, 0.0, 1.0)
        self._plot = Plot(800.0, height)
        self._last_hover = 0.0

        self._chart = fc.CandlestickChart(
            key="price-chart",
            spots=[],
            expand=True,
            visible=False,
            # The crosshair is the readout now, so the control's own touch
            # tooltip would only be a second, competing one.
            interactive=False,
            # No animation. It flatters a data swap and is actively wrong
            # under direct manipulation: every drag frame sets a new
            # window, so an animated chart spends its time easing towards
            # where the cursor *was*. Panning felt like dragging through
            # treacle until this came out.
            animation=None,
            horizontal_grid_lines=fc.ChartGridLines(
                color=ft.Colors.OUTLINE_VARIANT, width=1
            ),
        )
        self._overlay = cv.Canvas(shapes=[], expand=True, on_resize=self._resized)
        self._empty = ft.Text(
            "No price history for this pair.",
            size=12,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        self._gestures = ft.GestureDetector(
            content=ft.Stack(
                [
                    self._chart,
                    ft.Container(self._empty, alignment=ft.Alignment.CENTER),
                    # Last, so the crosshair draws over the candles. It does
                    # no hit-testing of its own -- the detector wraps
                    # everything -- so it never blocks a drag.
                    self._overlay,
                ],
                expand=True,
            ),
            expand=True,
            mouse_cursor=ft.MouseCursor.PRECISE,
            drag_interval=16,
            hover_interval=16,
            on_pan_update=self._panned,
            on_scroll=self._scrolled,
            on_hover=self._hovered,
            on_exit=self._left,
            on_double_tap=lambda _e: self.reset_view(),
        )
        super().__init__(
            content=self._gestures,
            height=height,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
            padding=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    # -- data -------------------------------------------------------------

    def set_candles(self, candles: list[Candle]) -> None:
        self._candles = candles or []
        self._empty.visible = not self._candles
        self._chart.visible = bool(self._candles)
        self._view = fit(self._candles)
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def reset_view(self) -> None:
        """Back to the whole series. Bound to double-tap."""
        self._view = fit(self._candles)
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def _apply_view(self) -> None:
        view = self._view
        self._chart.spots = build_spots(self._candles, view)
        self._chart.min_x, self._chart.max_x = view.x_min, view.x_max
        self._chart.min_y, self._chart.max_y = view.y_min, view.y_max
        self._chart.left_axis = price_axis(view)
        self._chart.bottom_axis = date_axis(self._candles, view)
        safe_update(self)

    # -- interaction ------------------------------------------------------

    def _resized(self, e: cv.CanvasResizeEvent) -> None:
        self._plot = Plot(e.width, e.height)

    def _panned(self, e: ft.DragUpdateEvent) -> None:
        """Drag the chart. The content follows the cursor, as it should."""
        if not self._candles:
            return
        delta = e.local_delta
        view = self._view.panned(
            -self._plot.dx(delta.x, self._view),
            # Screen y grows downward, so dragging down must raise prices.
            self._plot.dy(delta.y, self._view),
        )
        self._view = view.clamped(len(self._candles))
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def _scrolled(self, e: ft.ScrollEvent) -> None:
        """The wheel zooms in time, anchored on the candle under the cursor."""
        if not self._candles:
            return
        direction = 1.0 if e.scroll_delta.y > 0 else -1.0
        factor = 1.0 + direction * ZOOM_STEP
        focus = self._plot.data_x(e.local_position.x, self._view)
        zoomed = self._view.zoomed_x(factor, focus)
        if zoomed.x_span < MIN_VISIBLE and factor < 1.0:
            return  # already as tight as it goes
        self._view = zoomed.clamped(len(self._candles))
        self._apply_view()

    def _hovered(self, e: ft.HoverEvent) -> None:
        if not self._candles:
            return
        now = time.monotonic()
        if now - self._last_hover < HOVER_INTERVAL:
            return
        self._last_hover = now
        self._draw_crosshair(e.local_position.x, e.local_position.y)

    def _left(self, _e: ft.HoverEvent) -> None:
        self._clear_crosshair()

    def _draw_crosshair(self, px: float, py: float) -> None:
        self._overlay.shapes = crosshair_shapes(
            self._plot,
            self._view,
            self._candles,
            px,
            py,
            line_color=ft.Colors.OUTLINE,
            text_color=ft.Colors.ON_SURFACE,
            box_color=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )
        safe_update(self._overlay)

    def _clear_crosshair(self, redraw: bool = True) -> None:
        if not self._overlay.shapes:
            return
        self._overlay.shapes = []
        if redraw:
            safe_update(self._overlay)

    # -- header caption ---------------------------------------------------

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
