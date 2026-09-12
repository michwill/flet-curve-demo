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

import asyncio
import bisect
import math
import time
from collections.abc import Callable

import flet as ft
import flet.canvas as cv

from curve.format import compact_usd, percent
from curve.liquidity import Profile, Sample

from . import safe_update
from .typography import SMALL, TINY, text_width
from .viewport import ZOOM_STEP, Plot, Viewport

#: Roughly how many labels go on each axis.  The price axis holds to this at
#: every zoom: it is logarithmic, so a linear step over the visible range
#: thinned the lines out the further you zoomed, which is backwards -- a wider
#: window has *more* round numbers in it, not fewer.
PRICE_LABELS = 6
DEPTH_LABELS = 4

#: Multipliers a round price is built from, coarse first.  The ladder that is
#: fine enough to reach `PRICE_LABELS` across the window is the one used, so
#: the density holds whether the window is a tenth of a percent or six
#: decades.
LADDERS = (
    (1,),
    (1, 3),
    (1, 2, 5),
    (1, 1.5, 2, 3, 5, 7),
    (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 7, 8, 9),
)

#: How narrow the visible price window may get, as a log span.  A hundredth
#: of a percent is finer than any pool's own feature and far finer than the
#: solver's own tolerance, so below this the line is noise magnified.
MIN_LOG_SPAN = 1e-4

#: And how wide, so a scroll burst cannot leave the pool a dot in the middle.
MAX_LOG_SPAN = 12.0

#: The dashes in the spot line, in pixels on and off.
DASH_ON = 5.0
DASH_OFF = 4.0

#: The fee band's fill and its two edges.
SPREAD_FILL = 0.16
SPREAD_EDGE = 0.65

#: How thin the band may draw.  A four basis point fee across a window six
#: decades wide is a hundredth of a pixel, and a band nobody can see reads as
#: a broken feature rather than a small number.
MIN_SPREAD = 1.0

#: Room around the readout text, in pixels.
READOUT_PADDING = 8.0

#: How long to wait for the client to say it took a frame.  Past this the
#: chart stops asking rather than stall a second a frame.
ACK_TIMEOUT = 1.0

#: How often the client may send a hover, in milliseconds.  It sends at this
#: rate however slow the frames are, so on a slow client the events pile up
#: where Python cannot reach them -- `_pace` walks it out to the frame cost.
HOVER_FAST = 16
HOVER_SLOW = 250

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


def price_ticks(low: float, high: float, wanted: int = PRICE_LABELS
                ) -> list[float]:
    """Round prices across a log axis, at about the same density at any zoom.

    Round *prices* placed where their logarithm falls, rather than round
    logarithms: `1`, `2`, `5` read as prices and `10**0.3` does not.  The
    ladders run coarse to fine and the first one that fills the window wins.

    Below a ladder step there is nothing round left to land on -- a window
    from 0.999 to 1.001 contains exactly one of them -- so that falls back to
    an even step, which is what a window that narrow wants anyway.
    """
    if low <= 0.0 or high <= low:
        return []
    best: list[float] = []
    for ladder in LADDERS:
        ticks: list[float] = []
        power = math.floor(math.log10(low))
        while 10.0**power <= high:
            for step in ladder:
                value = step * 10.0**power
                if low <= value <= high:
                    ticks.append(value)
            power += 1
        if not best or abs(len(ticks) - wanted) < abs(len(best) - wanted):
            best = ticks
        if len(ticks) >= wanted:
            break
    # Whichever ladder came closest, whole.  Thinning a finer one to size
    # dropped whichever entries fell on the stride, and over 0.5 to 2 that
    # was 1.0 -- the one price on a stablecoin chart worth marking.
    # Only if it fills the axis.  Some windows hold few round numbers at any
    # ladder -- 0.9 to 1.1 has two, 2000 to 3000 has three -- and an even step
    # reads better there than four-fifths of an empty axis.
    if len(best) >= max(3, wanted // 2):
        return best
    span = high - low
    step = _nice_step(span, wanted)
    if step <= 0:
        return []
    ticks = []
    value = math.ceil(low / step) * step
    while value <= high:
        ticks.append(value)
        value += step
    return ticks


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
        #: Where the cursor is, and where the canvas was last drawn for.
        self._at: tuple[float, float] | None = None
        self._drawn: tuple[float, float] | None = None
        #: A frame owed for something other than the cursor -- a pan, a zoom,
        #: a resize. `_owed` rather than `_dirty`: Flet's own `Control._dirty`
        #: is a dict.
        self._owed = False
        self._painter: asyncio.Task | None = None
        #: What a frame has been costing, smoothed, in seconds.
        self._cost = HOVER_FAST / 1000.0
        #: The window and the finger position a gesture started from, and
        #: whether one is still in hand.
        self._held: Viewport | None = None
        self._held_at = 0.0
        self._gesturing = False
        #: `log(price)` per sample, for finding the one under the pointer.
        self._logs: list[float] = []
        #: Which sample the overlay is drawn for, and for which window.
        self._shown: tuple[int, tuple | None] | None = None
        #: The overlay, built once and moved.  Flet re-sends a shape it has
        #: not seen and skips one it has, so a frame that only moves these is
        #: a handful of numbers rather than seven controls torn down and put
        #: back up.
        band = ft.Paint(color=ft.Colors.with_opacity(
            SPREAD_FILL, ft.Colors.TERTIARY))
        rim = ft.Paint(color=ft.Colors.with_opacity(
            SPREAD_EDGE, ft.Colors.TERTIARY), stroke_width=1,
            style=ft.PaintingStyle.STROKE)
        self._cursor = cv.Line(0.0, 0.0, 0.0, 0.0, paint=ft.Paint(
            color=ft.Colors.ON_SURFACE_VARIANT, stroke_width=1))
        self._band = cv.Rect(0.0, 0.0, 1.0, 1.0, paint=band)
        self._edges = (cv.Line(0.0, 0.0, 0.0, 0.0, paint=rim),
                       cv.Line(0.0, 0.0, 0.0, 0.0, paint=rim))
        self._dot = cv.Circle(0.0, 0.0, 3, paint=ft.Paint(
            color=ft.Colors.PRIMARY))
        self._card = cv.Rect(0.0, 0.0, 1.0, 18.0, paint=ft.Paint(
            color=ft.Colors.with_opacity(0.93, ft.Colors.SURFACE)))
        self._label = cv.Text(0.0, 0.0, "", ft.TextStyle(
            size=TINY, color=ft.Colors.ON_SURFACE))
        #: Whether the client answers. Switched off by the first one that
        #: does not, so a silent client draws ungated instead of freezing.
        self._acks = True
        #: The chart under the cursor, and what it was drawn for.
        self._static: list[cv.Shape] = []
        self._static_key: tuple | None = None

        self._canvas = cv.Canvas(shapes=[], expand=True, on_resize=self._resized)
        #: The pointer's own canvas, over the curve's.  A patch names the
        #: canvas it belongs to, so moving the cursor leaves the curve's 64
        #: shapes untouched -- and it is walking them, not drawing them, that
        #: costs: a travelling frame was 5.3 ms of diffing nothing.
        self._overlay = cv.Canvas(shapes=[], expand=True)
        self._empty = ft.Text("", size=SMALL,
                              color=ft.Colors.ON_SURFACE_VARIANT)
        self._gestures = ft.GestureDetector(
            content=ft.Stack(
                [ft.Container(self._empty, alignment=ft.Alignment.CENTER),
                 self._canvas, self._overlay],
                expand=True,
            ),
            expand=True,
            mouse_cursor=ft.MouseCursor.PRECISE,
            drag_interval=16,
            hover_interval=16,
            on_pan_update=self._panned,
            on_pan_end=self._released,
            on_scale_start=self._grabbed,
            on_scale_update=self._scaled,
            on_scale_end=self._released,
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
        self._static_key = None
        self._shown = None
        self._logs = ([math.log(s.price) for s in self._profile.samples]
                      if self._profile else [])
        self._empty.value = "" if self._profile else "No curve for this pair."
        self._empty.visible = self._profile is None
        if self._profile is not None and not keep_view:
            self.reset_view()
        else:
            self._redraw()

    def say(self, message: str) -> None:
        self._profile = None
        self._static_key = None
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
        self._paint()

    def _panned(self, e: ft.DragUpdateEvent) -> None:
        """Drag the window with one pointer.

        Kept alongside the pinch handler because a drag recogniser honours
        `drag_interval` and a scale one has no throttle at all: the same drag
        measured 72 events through here against 551 through `_scaled`, and
        it is the count that costs -- the drawing is the same either way.
        """
        if self._profile is None:
            return
        delta = getattr(e, "local_delta", None)
        if delta is None or not delta.x:
            return
        self._view = self._view.panned(-self._plot.dx(delta.x, self._view), 0.0)
        self._paint()

    def _grabbed(self, e: ft.ScaleStartEvent) -> None:
        self._gesturing = True
        self._held = self._view
        self._held_at = e.local_focal_point.x

    def _released(self, _e: ft.ScaleEndEvent | ft.DragEndEvent | None = None
                  ) -> None:
        """Draw it properly, and ask for the curve the window now wants."""
        self._gesturing = False
        self._held = None
        self._settle()
        self._paint()

    def _scaled(self, e: ft.ScaleUpdateEvent) -> None:
        """One pointer drags the window, two pinch it. The wheel is desktop's.

        `on_scale_update` rather than `on_pan_update`, because Flutter will
        not take a pan recogniser and a scale recogniser on one detector --
        and scale reports a lone pointer as a drag, so one handler serves
        both.  Without it a phone can reach the chart but never zoom it.
        Only the two-finger case: one pointer is a drag, and a drag recogniser
        can be throttled where this cannot.

        Measured from where the gesture *began*, never from the last event.
        `scale` and the focal point are both given that way, and it is what
        makes an event safe to be late: a queued one describes the same
        window the newest does rather than moving it again, so the chart
        stops where the finger left it instead of drifting on while a backlog
        of deltas plays out.
        """
        held = self._held
        if self._profile is None or held is None or e.pointer_count < 2:
            return                      # one pointer is `_panned`'s business
        plot = self._plot
        spread = e.scale if e.scale > 0.0 else 1.0
        span = min(max(held.x_span / spread, MIN_LOG_SPAN), MAX_LOG_SPAN)
        # The price the fingers started on stays under them, as a map does it.
        anchor = plot.data_x(self._held_at, held)
        reach = (e.local_focal_point.x - plot.left) / plot.inner_width
        low = anchor - reach * span
        view = Viewport(low, low + span, held.y_min, held.y_max)
        if view == self._view:
            return
        self._view = view
        # Not `_settle` -- that asks the page to solve the curve again, and
        # the window is still moving.  `_released` asks once, at the end.
        self._paint()

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
        self._paint()

    def _hovered(self, e: ft.HoverEvent) -> None:
        self._at = (e.local_position.x, e.local_position.y)
        self._paint()

    def _pace(self) -> None:
        """Ask the client for hovers no faster than frames are coming out.

        The only backpressure there is.  The client sends on its own timer
        whatever the frames cost, so on a slow one the events pile up between
        Flutter and the worker -- somewhere Python can neither see nor drain.
        Widening its timer is what stops them being made in the first place.
        """
        want = min(max(int(self._cost * 1000.0), HOVER_FAST), HOVER_SLOW)
        if abs(want - (self._gestures.hover_interval or 0)) >= HOVER_FAST:
            self._gestures.hover_interval = want
            safe_update(self._gestures)

    def _left(self, _e: ft.HoverEvent) -> None:
        self._at = None
        self._paint()

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

    def _paint(self) -> None:
        """Ask for a frame, and get one -- not one per event that asked.

        Events that land while a frame is in flight are absorbed into `_at`,
        and the loop draws where the cursor got to -- once, not once each.

        """
        self._owed = True
        if self._painter is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._owed, self._drawn = False, self._at
            self._redraw()            # nothing to defer to: draw it here
            return
        self._painter = loop.create_task(self._painting())

    async def _painting(self) -> None:
        # Against the cursor rather than a flag alone: the loop runs until the
        # canvas shows where the cursor actually is, so nothing absorbed
        # mid-frame is lost and nothing is drawn twice.
        try:
            while self._owed or self._at != self._drawn:
                self._owed, self._drawn = False, self._at
                started = time.monotonic()
                self._redraw()
                await self._taken()
                # Smoothed, so one slow frame does not throttle the cursor and
                # one fast one does not undo a throttle that is warranted.
                self._cost += 0.25 * (time.monotonic() - started - self._cost)
                self._pace()
        finally:
            self._painter = None

    async def _taken(self) -> None:
        """Wait for the client to take the frame before drawing another.

        `update()` only queues a patch, so an ungated loop pushes frames at
        Python's speed into a client that repaints far slower, and the backlog
        is seconds of cursor positions already gone.  `clear_capture` is the
        cheapest round trip a canvas answers and nothing here captures, so the
        reply is all it is for -- and it rides the same connection behind the
        patch, so it cannot come back before the patch is consumed.

        It does not prove the pixels are on screen: Flet exposes no
        frame-complete callback, and the two calls that force a real rasterise
        draw a background of their own or ship a PNG.
        """
        if not self._acks:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._canvas.clear_capture(), ACK_TIMEOUT)
        except Exception:
            self._acks = False

    def _redraw(self) -> None:
        found = self._profile
        base = self._backdrop(found) if found is not None else []
        # `_backdrop` hands back the same list while the window holds still,
        # so this sends the curve only when the curve has actually moved.
        if base is not self._canvas.shapes:
            self._canvas.shapes = base
            safe_update(self._canvas)
        over = self._over(found, self._at)
        if over is not None:
            self._overlay.shapes = over
            safe_update(self._overlay)
        safe_update(self._empty)

    def _over(self, found: Profile | None,
              hover: tuple[float, float] | None) -> list[cv.Shape] | None:
        """What is drawn over the curve, or `None` if it would not change.

        Everything here sits on the sample under the pointer, so between two
        samples there is no new picture to send -- and at 160 samples across
        the width most of what a mouse reports falls between them.  Skipping
        those is what makes the band affordable to carry continuously.
        """
        plot = self._plot
        if found is None or not (hover and plot.contains(*hover)):
            if self._shown is None:
                return None
            self._shown = None
            return []
        k = self._nearest(hover[0])
        here = (k, self._static_key)
        if here == self._shown:
            return None
        self._shown = here
        return [*self._readout(found, found.samples[k]), self._cursor]

    def _nearest(self, px: float) -> int:
        """Which sample sits under `px`.

        Bisect over the prices, which are already in order, rather than a
        scan: this runs on every pointer move, and the scan it replaces
        measured `log` on all 160 of them each time.
        """
        want = self._plot.data_x(px, self._view)
        k = bisect.bisect_left(self._logs, want)
        if k <= 0:
            return 0
        if k >= len(self._logs):
            return len(self._logs) - 1
        return k if self._logs[k] - want < want - self._logs[k - 1] else k - 1

    def _backdrop(self, found: Profile) -> list[cv.Shape]:
        """The grid, the curve and the spot line, held between frames.

        A hover moves the readout and nothing else, and rebuilding the curve's
        own path for it was three quarters of the frame: Flet sends a shape it
        has not seen before and skips one it has, so handing back the same
        objects is what keeps them off the wire.
        """
        view = self._view
        key = (self._plot, view.x_min, view.x_max, view.y_min, view.y_max,
               self._unit, id(found))
        if key != self._static_key:
            self._static = (self._grid(found) + self._area(found)
                            + self._spot(found))
            self._static_key = key
        return self._static

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

        low, high = math.exp(view.x_min), math.exp(view.x_max)
        for tick in price_ticks(low, high):
            x = plot.pixel_x(math.log(tick), view)
            shapes.append(cv.Line(x, plot.top, x, plot.bottom, paint=faint))
            # Centred on the tick by the canvas, not by an offset guessed at
            # one label's width: nudging every label left by 28 centred the
            # six-character ones and left "1" a whole character clear of the
            # line it names, which on a stableswap is the one price a reader
            # is looking for.
            shapes.append(cv.Text(x, plot.bottom + 4, price_text(tick), label,
                                  alignment=ft.Alignment.TOP_CENTER))

        depth_step = _nice_step(view.y_max, DEPTH_LABELS)
        value = 0.0
        while value <= view.y_max and depth_step > 0:
            y = plot.pixel_y(value, view)
            shapes.append(cv.Line(plot.left, y, plot.right, y, paint=faint))
            shapes.append(cv.Text(2, y, self._amount(value), label,
                                  alignment=ft.Alignment.CENTER_LEFT))
            value += depth_step
        if self._unit:
            # Top right, not top left: the highest tick sits in the corner the
            # other way and the two were drawn over each other.
            shapes.append(cv.Text(plot.right, plot.top + 2,
                                  f"per 1% · {self._unit}", label,
                                  alignment=ft.Alignment.TOP_RIGHT))
        return shapes

    def _amount(self, value: float) -> str:
        """A depth, short enough for an axis. `1,000,000.00` is not."""
        return compact_usd(value, sign="$" if self._unit == "USD" else "")

    def _readout(self, found: Profile, at: Sample) -> list[cv.Shape]:
        """What the curve says at `at`, by moving the overlay onto it."""
        plot, view = self._plot, self._view
        x = plot.pixel_x(math.log(at.price), view)
        # The line, the dot and the card all sit on the sample, so a pointer
        # that has not crossed into the next one leaves the picture alone.
        self._cursor.x1 = self._cursor.x2 = x
        self._cursor.y1, self._cursor.y2 = plot.top, plot.bottom
        self._dot.x, self._dot.y = x, plot.pixel_y(at.depth, view)
        away = 1e2 * (at.price / found.spot - 1.0)
        text = (f"{price_text(at.price)}  ({away:+.2f}%)   "
                f"{self._amount(at.depth)}"
                f"{'' if self._unit == 'USD' else ' ' + self._unit}")
        if at.fee > 0:
            text += f"   fee {percent(at.fee * 1e2, places=4)}"
        width = text_width(text, TINY) + READOUT_PADDING
        left = min(max(x + 8, plot.left), max(plot.right - width, plot.left))
        self._card.x, self._card.y = left - 4, plot.top + 16
        self._card.width = width
        self._label.x, self._label.y = left, plot.top + 18
        self._label.value = text
        return [*self._spread(at), self._dot, self._card, self._label]

    def _spread(self, at: Sample) -> list[cv.Shape]:
        """The fee either side of the pointer, as the band it opens.

        A pool holding a price `p` and charging `f` sells at `p * (1 + f)` and
        buys at `p / (1 + f)`, so the fee is a width on the price axis: inside
        it there is no trade to make.  Multiplicative both ways, because the
        axis is.
        """
        if at.fee <= 0:
            return []
        plot, view = self._plot, self._view
        low = plot.pixel_x(math.log(at.price / (1 + at.fee)), view)
        high = plot.pixel_x(math.log(at.price * (1 + at.fee)), view)
        left, right = max(low, plot.left), min(high, plot.right)
        if right < left:
            return []
        self._band.x, self._band.y = left, plot.top
        self._band.width = max(right - left, MIN_SPREAD)
        self._band.height = plot.bottom - plot.top
        shapes: list[cv.Shape] = [self._band]
        # The edges join the list only while they are on screen, so the one
        # that is off it costs nothing rather than being hidden by a trick.
        for line, side in zip(self._edges, (low, high), strict=True):
            if plot.left <= side <= plot.right:
                line.x1 = line.x2 = side
                line.y1, line.y2 = plot.top, plot.bottom
                shapes.append(line)
        return shapes


__all__ = ["DepthChart", "price_text", "price_ticks"]
