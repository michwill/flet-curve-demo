"""The liquidity depth chart: a pool's own curve, drawn and readable.

Uniswap v3 puts liquidity in ticks and draws a bar per tick.  Curve's
invariants are smooth, so this is a filled line rather than a histogram, and
its shape is the curve's curvature -- flat curve, deep pool.

**The price axis is logarithmic**, and that is arithmetic rather than taste.
The height is liquidity *per 1% of price range*, so a pixel has to mean the
same 1% wherever it sits or the area under the line stops meaning anything.
Equal ratios, not equal differences.  `curve.liquidity` samples on the same
geometric grid, so the samples land evenly spaced across the width.

Drawn straight onto a canvas.  `flet-charts` has no filled-area series that
takes a log axis and a dashed marker, and the whole picture here is three
shapes -- a polygon, its outline, and one dashed line at the spot price.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import flet as ft
import flet.canvas as cv

from curve.format import token_amount
from curve.liquidity import Profile

from . import safe_update
from .typography import SMALL, TINY
from .viewport import ZOOM_STEP, Plot, Viewport

#: Roughly how many labels go on each axis.
PRICE_LABELS = 5
DEPTH_LABELS = 4

#: Hover fires per mouse move and each one is a round trip into Python.
HOVER_INTERVAL = 0.04

#: How narrow the visible price window may get, as a log span.  A hundredth
#: of a percent is finer than any pool's own feature and far finer than the
#: solver's own tolerance, so below this the line is noise magnified.
MIN_LOG_SPAN = 1e-4

#: And how wide, so a scroll burst cannot leave the pool a dot in the middle.
MAX_LOG_SPAN = 12.0

#: The dashes in the spot line, in pixels on and off.
DASH_ON = 5.0
DASH_OFF = 4.0

def _nice_step(span: float, wanted: int) -> float:
    """A round number near `span / wanted`, for axis ticks."""
    if span <= 0 or wanted <= 0:
        return 1.0
    rough = span / wanted
    power = 10.0 ** math.floor(math.log10(rough))
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if rough <= step * power:
            return step * power
    return 10.0 * power


def price_text(price: float) -> str:
    """A price with the decimals it actually needs."""
    if price <= 0:
        return "0"
    if price >= 1000:
        return f"{price:,.0f}"
    places = max(2, min(8, math.ceil(-math.log10(price)) + 4))
    return f"{price:,.{places}f}".rstrip("0").rstrip(".")


class DepthChart(ft.Container):
    """A pool's liquidity against price, draggable, zoomable and readable."""

    def __init__(self, height: float = 340,
                 on_window_change: Callable[[float, float], None] | None = None
                 ) -> None:
        self._profile: Profile | None = None
        self._unit = ""
        self._plot = Plot(800.0, height)
        self._view = Viewport(0.0, 1.0, 0.0, 1.0)
        self._on_window_change = on_window_change
        self._last_hover = 0.0

        self._canvas = cv.Canvas(shapes=[], expand=True, on_resize=self._resized)
        self._empty = ft.Text("", size=SMALL,
                              color=ft.Colors.ON_SURFACE_VARIANT)
        self._gestures = ft.GestureDetector(
            content=ft.Stack(
                [ft.Container(self._empty, alignment=ft.Alignment.CENTER),
                 self._canvas],
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
            padding=ft.Padding.only(left=8, top=8, bottom=8, right=24),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    # -- data -------------------------------------------------------------

    def show(self, profile: Profile | None, unit: str = "",
             keep_view: bool = False) -> None:
        """Draw a profile, or say there is nothing to draw."""
        self._profile = profile if profile and profile.samples else None
        self._unit = unit
        self._empty.value = "" if self._profile else "No curve for this pair."
        self._empty.visible = self._profile is None
        if self._profile is not None and not keep_view:
            self.reset_view()
        else:
            self._redraw()

    def say(self, message: str) -> None:
        self._profile = None
        self._empty.value = message
        self._empty.visible = True
        self._redraw()

    def reset_view(self) -> None:
        """Frame the whole profile, with the depth axis from zero."""
        found = self._profile
        if found is None:
            self._redraw()
            return
        low = math.log(found.samples[0].price)
        high = math.log(found.samples[-1].price)
        self._view = Viewport(low, high, 0.0, found.peak * 1.08 or 1.0)
        self._redraw()

    @property
    def window(self) -> tuple[float, float]:
        """The visible price range."""
        return math.exp(self._view.x_min), math.exp(self._view.x_max)

    # -- gestures ---------------------------------------------------------

    def _resized(self, e: cv.CanvasResizeEvent) -> None:
        self._plot = Plot(float(e.width or 800.0), float(e.height or 340.0))
        self._redraw()

    def _panned(self, e: ft.DragUpdateEvent) -> None:
        if self._profile is None:
            return
        delta = e.local_delta
        if delta is None:
            return
        self._view = self._view.panned(-self._plot.dx(delta.x, self._view), 0.0)
        self._settle()
        self._redraw()

    def _scrolled(self, e: ft.ScrollEvent) -> None:
        """Zoom the price axis about the pointer.

        Only the price axis: the height is a quantity with a zero, and a depth
        window that does not start there invites reading a shoulder as a peak.
        """
        if self._profile is None:
            return
        direction = 1.0 if e.scroll_delta.y > 0 else -1.0
        factor = 1.0 + direction * ZOOM_STEP
        focus = self._plot.data_x(e.local_position.x, self._view)
        zoomed = self._view.zoomed_x(factor, focus)
        span = zoomed.x_span
        if not MIN_LOG_SPAN <= span <= MAX_LOG_SPAN:
            return
        self._view = zoomed
        self._settle()
        self._redraw()

    def _hovered(self, e: ft.HoverEvent) -> None:
        now = time.monotonic()
        if now - self._last_hover < HOVER_INTERVAL:
            return
        self._last_hover = now
        self._redraw(hover=(e.local_position.x, e.local_position.y))

    def _left(self, _e: ft.HoverEvent) -> None:
        self._redraw()

    def _settle(self) -> None:
        """Say the window moved, so the profile can be resolved to suit it.

        Told rather than debounced here: a wheel arrives as a burst of notches
        and each one would otherwise cost a few hundred invariant solves, but
        the coalescing belongs to whoever owns the task that does the solving.
        The chart says what it is showing; the page decides how often to act.
        """
        if self._on_window_change is not None:
            low, high = self.window
            self._on_window_change(low, high)

    # -- drawing ----------------------------------------------------------

    def _redraw(self, hover: tuple[float, float] | None = None) -> None:
        self._canvas.shapes = self._shapes(hover)
        safe_update(self._canvas)
        safe_update(self._empty)

    def _shapes(self, hover: tuple[float, float] | None) -> list[cv.Shape]:
        found = self._profile
        if found is None:
            return []
        plot = self._plot
        shapes: list[cv.Shape] = []
        shapes += self._grid(found)
        shapes += self._area(found)
        shapes += self._spot(found)
        if hover and plot.contains(*hover):
            shapes += self._readout(found, *hover)
        return shapes

    def _area(self, found: Profile) -> list[cv.Shape]:
        """The filled curve, and its outline on top."""
        plot, view = self._plot, self._view
        points: list[tuple[float, float]] = []
        for sample in found.samples:
            x = plot.pixel_x(math.log(sample.price), view)
            if x < plot.left - 4 or x > plot.right + 4:
                continue
            points.append((x, plot.pixel_y(sample.depth, view)))
        if len(points) < 2:
            return []
        floor = plot.pixel_y(0.0, view)
        body: list[cv.Path.PathElement] = [cv.Path.MoveTo(points[0][0], floor)]
        body += [cv.Path.LineTo(x, y) for x, y in points]
        body.append(cv.Path.LineTo(points[-1][0], floor))
        body.append(cv.Path.Close())
        outline: list[cv.Path.PathElement] = [cv.Path.MoveTo(*points[0])]
        outline += [cv.Path.LineTo(x, y) for x, y in points[1:]]
        return [
            cv.Path(body, paint=ft.Paint(
                color=ft.Colors.with_opacity(0.22, ft.Colors.PRIMARY),
                style=ft.PaintingStyle.FILL)),
            cv.Path(outline, paint=ft.Paint(
                color=ft.Colors.PRIMARY, stroke_width=1.6,
                style=ft.PaintingStyle.STROKE)),
        ]

    def _spot(self, found: Profile) -> list[cv.Shape]:
        """A dashed line where the pool is trading now."""
        plot, view = self._plot, self._view
        x = plot.pixel_x(math.log(found.spot), view)
        if not plot.left - 1 <= x <= plot.right + 1:
            return []
        paint = ft.Paint(color=ft.Colors.ON_SURFACE, stroke_width=1.2,
                         style=ft.PaintingStyle.STROKE)
        shapes: list[cv.Shape] = []
        y = plot.top
        while y < plot.bottom:
            shapes.append(cv.Line(x, y, x, min(y + DASH_ON, plot.bottom),
                                  paint=paint))
            y += DASH_ON + DASH_OFF
        shapes.append(cv.Text(
            x + 4, plot.top + 2, "spot",
            ft.TextStyle(size=TINY, color=ft.Colors.ON_SURFACE_VARIANT)))
        return shapes

    def _grid(self, found: Profile) -> list[cv.Shape]:
        """Ticks on both axes: prices along the bottom, depth up the side."""
        plot, view = self._plot, self._view
        faint = ft.Paint(color=ft.Colors.OUTLINE_VARIANT, stroke_width=1)
        label = ft.TextStyle(size=TINY, color=ft.Colors.ON_SURFACE_VARIANT)
        shapes: list[cv.Shape] = []

        # Price: the axis is log, so the ticks are round *prices* placed where
        # their logarithm falls, rather than round logarithms.
        low, high = math.exp(view.x_min), math.exp(view.x_max)
        step = _nice_step(high - low, PRICE_LABELS)
        tick = math.ceil(low / step) * step
        while tick <= high and step > 0:
            x = plot.pixel_x(math.log(tick), view)
            shapes.append(cv.Line(x, plot.top, x, plot.bottom, paint=faint))
            shapes.append(cv.Text(x - 28, plot.bottom + 4, price_text(tick),
                                  label))
            tick += step

        depth_step = _nice_step(view.y_max, DEPTH_LABELS)
        value = 0.0
        while value <= view.y_max and depth_step > 0:
            y = plot.pixel_y(value, view)
            shapes.append(cv.Line(plot.left, y, plot.right, y, paint=faint))
            shapes.append(cv.Text(2, y - 6, token_amount(value), label))
            value += depth_step
        if self._unit:
            shapes.append(cv.Text(2, plot.top + 2, self._unit, label))
        return shapes

    def _readout(self, found: Profile, px: float, py: float) -> list[cv.Shape]:
        """What the curve says at the pointer."""
        plot, view = self._plot, self._view
        price = math.exp(plot.data_x(px, view))
        nearest = min(found.samples,
                      key=lambda s: abs(math.log(s.price / price)))
        x = plot.pixel_x(math.log(nearest.price), view)
        y = plot.pixel_y(nearest.depth, view)
        paint = ft.Paint(color=ft.Colors.ON_SURFACE_VARIANT, stroke_width=1)
        away = 1e2 * (nearest.price / found.spot - 1.0)
        text = (f"{price_text(nearest.price)}  ({away:+.2f}%)   "
                f"{token_amount(nearest.depth)} {self._unit}")
        left = min(max(px + 8, plot.left), max(plot.right - 210, plot.left))
        return [
            cv.Line(x, plot.top, x, plot.bottom, paint=paint),
            cv.Circle(x, y, 3, paint=ft.Paint(color=ft.Colors.PRIMARY)),
            cv.Rect(left - 4, plot.top + 16, 206, 18, paint=ft.Paint(
                color=ft.Colors.with_opacity(0.86, ft.Colors.SURFACE))),
            cv.Text(left, plot.top + 18, text,
                    ft.TextStyle(size=TINY, color=ft.Colors.ON_SURFACE)),
        ]


__all__ = ["DepthChart", "price_text"]
