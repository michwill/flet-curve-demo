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
from collections.abc import Callable

import flet as ft
import flet.canvas as cv
import flet_charts as fc

from curve.api import Candle
from curve.format import token_amount

from . import safe_update
from .typography import SMALL, TINY
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

#: Target pixels per candle slot.
#:
#: `CandlestickChart` draws candle bodies at a fixed width -- measured at
#: ~3 logical pixels, and unchanged whether it is handed 20 spots or 365 --
#: so the only lever on how the chart *looks* is how many candles share the
#: plot width. Too many and they merge into a block; too few and the gaps
#: dwarf the candles. At ~5.5px a 3px candle sits in a 2.5px gap, which
#: reads as a candle chart rather than a bar code or a dotted line.
#:
#: The count follows from this and the plot width, so a candle is the same
#: size at every candle size, and a wider chart shows *more* candles rather
#: than the same ones stretched.
TARGET_PITCH_PX = 5.5

#: Bounds on that count, for very small or very wide charts.
MIN_CANDLES = 40
MAX_CANDLES = 400

#: How much the capacity must change before the series is refetched. Without
#: a threshold, every pixel of a window drag would trigger a request.
CAPACITY_TOLERANCE = 0.25

#: Smallest high-low extent a candle is drawn with, in pixels.
#:
#: `CandlestickChart` draws nothing at all for a candle whose high and low
#: land on the same pixel -- no doji line, no dot, just a gap. On a stable
#: pool that is most of them: Strategic USD Reserves over 7 days has 101 of
#: 169 hourly candles under a pixel, which is why the chart looked like it
#: was missing data. It was not; those candles were rendering as nothing.
#:
#: The floor is applied symmetrically about the candle's midpoint, and only
#: to the copy handed to the chart. `self._candles` keeps the true values,
#: so the crosshair still reads out real numbers.
MIN_CANDLE_PX = 1.5


def price_decimals(span: float) -> int:
    """How many decimals a value needs across a given span."""
    if span <= 0:
        return 2
    return max(2, min(10, math.ceil(-math.log10(span)) + 2))


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


#: How far beyond the body range a wick may reach and still set the scale,
#: in multiples of that range. Scale-free on purpose: 3x the body span is a
#: long wick on any pool, whereas a fixed percentage would trim real moves
#: on a volatile pool and admit nonsense on a pegged one.
WICK_HEADROOM = 3.0


def price_bounds(candles: list[Candle]) -> tuple[float, float]:
    """The price range to show for a set of candles, padded by 4%.

    Fitted to the candle *bodies* plus any wick within `WICK_HEADROOM` of
    them, because one bad wick otherwise sets the whole scale. Strategic
    USD Reserves has a daily candle whose low is 0.024 against a body of
    1.0158 -- an API glitch, not a two-cent trade in a USDC/USDT pool --
    and it flattened 200 days of history into a line at the top.

    A body is never excluded, and the rule is relative to how much the
    series actually moves, so a genuine 1.2% dip on the same pool still
    sets the scale while the 97% one does not. The outlier is not deleted:
    it is drawn clipped, and panning down reaches it.

    A flat series -- every price identical, which pegged stable pairs
    really do produce -- gets an invented range rather than a zero-height
    axis the chart cannot scale.
    """
    if not candles:
        return 0.0, 1.0

    body_low = min(min(c.open, c.close) for c in candles)
    body_high = max(max(c.open, c.close) for c in candles)
    body_span = (body_high - body_low) or abs(body_high) * 0.001 or 0.001

    floor = body_low - WICK_HEADROOM * body_span
    ceiling = body_high + WICK_HEADROOM * body_span
    low = min([c.low for c in candles if c.low >= floor], default=body_low)
    high = max([c.high for c in candles if c.high <= ceiling], default=body_high)
    low, high = min(low, body_low), max(high, body_high)

    if high <= low:
        spread = abs(high) * 0.001 or 0.001
        return low - spread, high + spread
    margin = (high - low) * 0.04
    return low - margin, high + margin


def visible_slice(candles: list[Candle], view: Viewport) -> list[Candle]:
    """The candles inside the window, for refitting the price axis."""
    if not candles:
        return []
    start = max(0, math.floor(view.x_min))
    end = min(len(candles), math.ceil(view.x_max) + 1)
    return candles[start:end] or candles


def fit(candles: list[Candle]) -> Viewport:
    """The window showing the whole series."""
    low, high = price_bounds(candles)
    return Viewport(-0.5, max(len(candles) - 0.5, 0.5), low, high)


def _axis_label(text: str) -> ft.Control:
    return ft.Text(text, size=TINY, color=ft.Colors.ON_SURFACE_VARIANT)


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
    first = max(0, math.floor(view.x_min))
    last = min(len(candles) - 1, math.ceil(view.x_max))
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
    start = max(0, math.floor(view.x_min) - SPOT_MARGIN)
    end = min(len(candles), math.ceil(view.x_max) + SPOT_MARGIN + 1)
    return start, max(start, end)


def build_spots(
    candles: list[Candle],
    view: Viewport | None = None,
    min_extent: float = 0.0,
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

    `min_extent` is the smallest high-low a candle may be drawn with, in
    price units -- see `MIN_CANDLE_PX`. Anything flatter is widened about
    its own midpoint so it renders as a hairline instead of vanishing.

    `x` stays the index into the *full* series, so the viewport, the axis
    labels and the crosshair all keep speaking one coordinate system.
    """
    if view is None:
        start, end = 0, len(candles)
    else:
        start, end = visible_range(candles, view)

    spots = []
    for index in range(start, end):
        candle = candles[index]
        high, low = candle.high, candle.low
        if min_extent > 0 and high - low < min_extent:
            middle = (high + low) / 2
            high, low = middle + min_extent / 2, middle - min_extent / 2
        spots.append(
            fc.CandlestickChartSpot(
                x=index,
                open=candle.open,
                high=high,
                low=low,
                close=candle.close,
                # The crosshair readout replaces the control's own tooltip.
                show_tooltip=False,
            )
        )
    return spots


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
            style=ft.TextStyle(size=TINY, color=text_color, weight=ft.FontWeight.BOLD),
        )
    )

    index = round(plot.data_x(px, view))
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
                style=ft.TextStyle(size=TINY, color=text_color),
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
                style=ft.TextStyle(size=TINY, color=text_color),
            )
        )
    return shapes


class CandleChart(ft.Container):
    """A candlestick chart you can drag, zoom and read off.

    Drag pans in both axes, the wheel zooms in time about the cursor,
    hovering shows a crosshair with the price and time under it, and a
    double-tap refits the whole series.
    """

    def __init__(
        self, height: float = 340, on_capacity_change: Callable[[], None] | None = None
    ) -> None:
        self._candles: list[Candle] = []
        self._view = Viewport(0.0, 1.0, 0.0, 1.0)
        self._plot = Plot(800.0, height)
        self._last_hover = 0.0
        self._on_capacity_change = on_capacity_change
        # Seeded from the default plot size rather than left at zero: the
        # first real layout must be *compared* against something, or a
        # chart that opens narrow keeps the wide guess it was built with.
        self._last_capacity = 0  # set below, once _plot exists
        # Price follows the visible candles until the user takes over. This
        # is what makes zooming in useful: without it, ten candles keep the
        # whole series' price range and stay squashed into a few pixels.
        # pyqtgraph calls the same thing `enableAutoRange`.
        self._auto_price = True

        self._chart = fc.CandlestickChart(
            key="price-chart",
            spots=[],
            expand=True,
            visible=False,
            # The crosshair is the readout now, so the control's own touch
            # tooltip would only be a second, competing one.
            interactive=False,
            # Zero animation, spelled explicitly.
            #
            # `AnimationValue` is `Union[bool, int, Animation]` -- **not**
            # Optional -- and the field defaults to a 150ms linear tween.
            # Passing `None` does not disable it; it falls through to that
            # default, which is why panning still visibly trailed the
            # cursor after the first attempt at turning it off. A zero
            # Duration is the way to actually mean none.
            #
            # It matters because every drag frame sets a new window, so any
            # tween at all leaves the chart easing towards where the cursor
            # *was* rather than where it is.
            animation=ft.Animation(
                duration=ft.Duration(milliseconds=0),
                curve=ft.AnimationCurve.LINEAR,
            ),
            horizontal_grid_lines=fc.ChartGridLines(
                color=ft.Colors.OUTLINE_VARIANT, width=1
            ),
        )
        self._overlay = cv.Canvas(shapes=[], expand=True, on_resize=self._resized)
        self._last_capacity = self.candle_capacity()
        self._empty = ft.Text(
            "No price history for this pair.",
            size=SMALL,
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
            # Wider on the right: the last time label is centred on the last
            # candle, which sits at the very edge, so half of it hangs past
            # the plot and would be clipped.
            padding=ft.Padding.only(left=8, top=8, bottom=8, right=24),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    # -- data -------------------------------------------------------------

    def set_candles(self, candles: list[Candle]) -> None:
        self._candles = candles or []
        self._auto_price = True
        self._empty.visible = not self._candles
        self._chart.visible = bool(self._candles)
        self._view = fit(self._candles)
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def reset_view(self) -> None:
        """Back to the whole series, and back to auto price. Double-tap."""
        self._auto_price = True
        self._view = fit(self._candles)
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def _refit_price(self) -> None:
        """Rescale price to whatever is on screen, unless the user opted out."""
        if not self._auto_price or not self._candles:
            return
        low, high = price_bounds(visible_slice(self._candles, self._view))
        self._view = self._view.with_y(low, high)

    def _apply_view(self) -> None:
        view = self._view
        # One pixel's worth of price, so a flat candle still draws.
        min_extent = view.y_span / self._plot.inner_height * MIN_CANDLE_PX
        self._chart.spots = build_spots(self._candles, view, min_extent)
        self._chart.min_x, self._chart.max_x = view.x_min, view.x_max
        self._chart.min_y, self._chart.max_y = view.y_min, view.y_max
        self._chart.left_axis = price_axis(view)
        self._chart.bottom_axis = date_axis(self._candles, view)
        safe_update(self)

    # -- interaction ------------------------------------------------------

    def candle_capacity(self) -> int:
        """How many candles this chart has room for at the target pitch."""
        raw = int(self._plot.inner_width / TARGET_PITCH_PX)
        return max(MIN_CANDLES, min(MAX_CANDLES, raw))

    def _resized(self, e: cv.CanvasResizeEvent) -> None:
        self._plot = Plot(e.width, e.height)
        capacity = self.candle_capacity()
        # Refetch only when the width changed enough to matter, so dragging
        # a window edge does not fire a request per pixel.
        if self._last_capacity and self._on_capacity_change:
            change = abs(capacity - self._last_capacity) / self._last_capacity
            if change > CAPACITY_TOLERANCE:
                self._last_capacity = capacity
                self._on_capacity_change()
                return
        self._last_capacity = capacity

    def _panned(self, e: ft.DragUpdateEvent) -> None:
        """Drag the chart. The content follows the cursor, as it should."""
        if not self._candles:
            return
        delta = e.local_delta
        if delta is None:
            return
        view = self._view.panned(
            -self._plot.dx(delta.x, self._view),
            # Screen y grows downward, so dragging down must raise prices.
            self._plot.dy(delta.y, self._view),
        )
        # Dragging is the user taking the price axis into their own hands.
        if delta.y:
            self._auto_price = False
        self._view = view.clamped(len(self._candles))
        self._clear_crosshair(redraw=False)
        self._apply_view()

    def _scrolled(self, e: ft.ScrollEvent) -> None:
        """The wheel zooms in time, anchored on the candle under the cursor."""
        if not self._candles:
            return
        direction = 1.0 if e.scroll_delta.y > 0 else -1.0
        factor = 1.0 + direction * ZOOM_STEP

        # Over the price gutter the wheel scales price, the way a trading
        # chart does it -- there are no modifier keys on a Flet scroll
        # event, so position is the only thing to dispatch on.
        if e.local_position.x < self._plot.left:
            self._auto_price = False
            focus = self._plot.data_y(e.local_position.y, self._view)
            self._view = self._view.zoomed_y(factor, focus)
            self._apply_view()
            return

        focus = self._plot.data_x(e.local_position.x, self._view)
        zoomed = self._view.zoomed_x(factor, focus)
        if zoomed.x_span < MIN_VISIBLE and factor < 1.0:
            return  # already as tight as it goes
        self._view = zoomed.clamped(len(self._candles))
        self._refit_price()
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
