"""The depth chart's axis, which is logarithmic and has to stay readable."""

from __future__ import annotations

import asyncio
import math
from itertools import pairwise

import flet as ft
import pytest

from curve.liquidity import Profile, Sample
from ui.depthchart import (
    HOVER_FAST,
    HOVER_SLOW,
    MIN_SPREAD,
    PRICE_LABELS,
    SETTLE,
    DepthChart,
    price_text,
    price_ticks,
)

#: Windows the chart is actually asked to draw, from a stableswap zoomed all
#: the way in to a crypto pair zoomed all the way out.
WINDOWS = (
    (0.999, 1.001),
    (0.98, 1.02),
    (0.9, 1.1),
    (0.5, 2.0),
    (0.1, 10.0),
    (1e-3, 1e3),
    (2000.0, 3000.0),
    (78_000.0, 80_000.0),
)


def test_the_axis_keeps_its_density_at_every_zoom():
    """Zooming out must not thin the grid.  A wider window holds *more* round
    numbers, not fewer -- an even step over the visible range read the other
    way round, because the axis is logarithmic and the step was not.
    """
    for low, high in WINDOWS:
        ticks = price_ticks(low, high)
        assert 3 <= len(ticks) <= PRICE_LABELS * 2, f"{low}..{high}: {ticks}"


def test_every_tick_is_inside_the_window():
    for low, high in WINDOWS:
        assert all(low <= tick <= high for tick in price_ticks(low, high))


def test_ticks_come_out_in_order_and_without_repeats():
    for low, high in WINDOWS:
        ticks = price_ticks(low, high)
        assert ticks == sorted(ticks)
        assert len(set(ticks)) == len(ticks)


def test_one_is_kept_wherever_it_falls_in_the_window():
    """Thinning a finer ladder to size dropped whatever landed on the stride,
    and over 0.5 to 2 that was 1.0 -- the one price on a stablecoin chart
    worth marking."""
    for low, high in ((0.5, 2.0), (0.9, 1.1), (0.98, 1.02), (0.1, 10.0)):
        assert any(abs(tick - 1.0) < 1e-12 for tick in price_ticks(low, high))


def test_a_window_with_no_room_left_is_refused():
    assert price_ticks(0.0, 1.0) == []
    assert price_ticks(1.0, 1.0) == []
    assert price_ticks(-1.0, 1.0) == []


def test_a_price_is_written_with_the_decimals_it_needs():
    assert price_text(79_000.0) == "79,000"
    assert price_text(1.0) == "1"
    assert price_text(0.0004065) == "0.0004065"
    assert price_text(0) == "0"


def test_the_ticks_are_evenly_spread_across_a_log_axis():
    """They are round prices, so the spacing is not exact -- but no gap should
    swallow half the axis while the rest crowd into a corner.
    """
    low, high = 0.1, 10.0
    ticks = price_ticks(low, high)
    span = math.log(high / low)
    gaps = [math.log(b / a) for a, b in pairwise(ticks)]
    assert max(gaps) <= span / 2


def built_chart() -> DepthChart:
    """A chart framing a stableswap around 1, where the ticks are a mix of
    one-character and six-character labels."""
    samples = tuple(
        Sample(price=0.998 + i * 0.0002, depth=1_000.0 + i)
        for i in range(21)
    )
    chart = DepthChart()
    chart.show(Profile(samples=samples, spot=1.0, pair=(0, 1)), unit="USD")
    return chart


def label_shapes(chart):
    """The price labels along the bottom, with the gridline each names."""
    import flet.canvas as cv

    shapes = chart._grid(chart._profile)
    lines = [s for s in shapes if isinstance(s, cv.Line) and s.y1 != s.y2]
    texts = [s for s in shapes if isinstance(s, cv.Text)]
    return lines, texts[:len(lines)]


def test_a_price_label_sits_on_the_gridline_it_names() -> None:
    """Offset by a guess at one label's width, "1" landed a whole character
    clear of the line -- and on a stableswap that is the price a reader came
    to find."""
    import flet as ft

    chart = built_chart()
    lines, texts = label_shapes(chart)

    assert lines and len(texts) == len(lines)
    for line, text in zip(lines, texts, strict=True):
        assert text.x == line.x1
        assert text.alignment == ft.Alignment.TOP_CENTER


def test_and_does_so_whatever_the_label_is_long() -> None:
    """A one-character tick and a six-character one are placed the same way,
    which a fixed nudge cannot do."""
    chart = built_chart()
    _, texts = label_shapes(chart)
    widths = {len(t.value or "") for t in texts}

    assert len(widths) > 1  # the axis really does mix label lengths
    assert all(t.alignment == texts[0].alignment for t in texts)


def fee_chart(fee: float = 0.004) -> DepthChart:
    """The same chart, on a pool that charges something."""
    samples = tuple(
        Sample(price=0.98 + i * 0.002, depth=1_000.0 + i, fee=fee)
        for i in range(21)
    )
    chart = DepthChart()
    chart.show(Profile(samples=samples, spot=1.0, pair=(0, 1)), unit="USD")
    return chart


def test_the_band_spans_the_fee_either_side_of_the_price() -> None:
    """A pool holding `p` and charging `f` sells at `p * (1 + f)` and buys at
    `p / (1 + f)`, so the band is that width on the price axis."""
    import flet.canvas as cv

    chart = fee_chart(0.004)
    plot, view = chart._plot, chart._view
    shapes = chart._spread(Sample(price=1.0, depth=1_000.0, fee=0.004))
    rects = [s for s in shapes if isinstance(s, cv.Rect)]
    edges = sorted(s.x1 for s in shapes if isinstance(s, cv.Line))

    low = plot.pixel_x(math.log(1.0 / 1.004), view)
    high = plot.pixel_x(math.log(1.004), view)
    assert len(rects) == 1
    assert rects[0].x == pytest.approx(low)
    assert rects[0].x + rects[0].width == pytest.approx(high)
    assert edges == pytest.approx([low, high])


def test_a_band_too_narrow_to_see_is_still_drawn() -> None:
    """A four basis point fee across six decades is a hundredth of a pixel,
    and a band nobody can see reads as a broken feature."""
    import flet.canvas as cv

    chart = fee_chart(4e-5)
    chart._view = chart._view.zoomed_x(1e5, math.log(1.0))
    rects = [s for s in chart._spread(Sample(price=1.0, depth=1.0, fee=4e-5))
             if isinstance(s, cv.Rect)]

    assert rects and rects[0].width >= MIN_SPREAD


def test_a_pool_with_no_fee_to_report_gets_no_band() -> None:
    chart = fee_chart(0.0)
    assert chart._spread(Sample(price=1.0, depth=1_000.0, fee=0.0)) == []


def test_the_readout_says_what_the_fee_is_there() -> None:
    import flet.canvas as cv

    chart = fee_chart(0.004)
    x = chart._plot.pixel_x(math.log(1.0), chart._view)
    texts = [s.value for s in chart._readout(chart._profile, x, 50.0)
             if isinstance(s, cv.Text)]

    assert texts and "fee 0.4000%" in texts[0]


def test_and_leaves_it_out_where_there_is_none() -> None:
    import flet.canvas as cv

    chart = fee_chart(0.0)
    x = chart._plot.pixel_x(math.log(1.0), chart._view)
    texts = [s.value for s in chart._readout(chart._profile, x, 50.0)
             if isinstance(s, cv.Text)]

    assert texts and "fee" not in texts[0]


class Move:
    """A hover event at one point, as Flet delivers it."""

    def __init__(self, x: float, y: float, touch: bool = False) -> None:
        self.local_position = type("At", (), {"x": x, "y": y})()
        self.kind = (ft.PointerDeviceType.TOUCH if touch
                     else ft.PointerDeviceType.MOUSE)


async def test_a_sweep_draws_once_and_at_the_end_of_it() -> None:
    """The band lagged behind the cursor because every position of a sweep
    was queued and drawn in turn.  Only the newest is worth a frame."""
    chart = fee_chart()
    drawn: list[tuple[float, float] | None] = []
    chart._redraw = lambda: drawn.append(chart._at)  # type: ignore[method-assign]

    for x in range(200, 260, 4):
        chart._hovered(Move(float(x), 120.0))
    for _ in range(6):
        await asyncio.sleep(0)

    assert len(drawn) < 5, drawn          # not one per event
    assert drawn[-1] == (256.0, 120.0)    # and it ends where the cursor did


async def test_a_frame_owed_while_one_is_drawn_is_not_lost() -> None:
    """Coalescing must not swallow the last event: whatever arrives during a
    frame is what the next one draws."""
    chart = fee_chart()
    drawn: list[tuple[float, float] | None] = []

    def redraw() -> None:
        drawn.append(chart._at)
        if len(drawn) == 1:               # the cursor moved mid-frame
            chart._hovered(Move(400.0, 90.0))

    chart._redraw = redraw  # type: ignore[method-assign]
    chart._hovered(Move(300.0, 120.0))
    for _ in range(8):
        await asyncio.sleep(0)

    assert drawn == [(300.0, 120.0), (400.0, 90.0)]


async def test_leaving_the_chart_clears_the_readout() -> None:
    chart = fee_chart()
    chart._hovered(Move(300.0, 120.0))
    chart._left(None)
    for _ in range(3):
        await asyncio.sleep(0)

    assert chart._at is None


def test_without_a_loop_a_frame_is_drawn_on_the_spot() -> None:
    """`show` and the tests run outside one, and a chart that waited for a
    task that would never be scheduled would simply stay blank."""
    chart = fee_chart()
    drawn: list[tuple[float, float] | None] = []
    chart._redraw = lambda: drawn.append(chart._at)  # type: ignore[method-assign]

    chart._hovered(Move(300.0, 120.0))

    assert drawn == [(300.0, 120.0)]
    assert chart._painter is None


def test_the_backdrop_is_held_while_only_the_cursor_moves() -> None:
    """Rebuilding the curve's path for a hover was three quarters of the
    frame, and Flet re-sends a shape object it has not seen before."""
    chart = fee_chart()
    first = chart._backdrop(chart._profile)

    assert chart._backdrop(chart._profile) is first

    chart._view = chart._view.panned(0.001, 0.0)
    assert chart._backdrop(chart._profile) is not first


def test_and_is_rebuilt_when_the_pool_changes() -> None:
    chart = fee_chart()
    first = chart._backdrop(chart._profile)
    chart.show(chart._profile, unit="USD", keep_view=True)

    assert chart._backdrop(chart._profile) is not first



async def test_a_pan_still_redraws_though_the_cursor_has_not_moved() -> None:
    """The loop runs on the cursor, so a view change needs its own say."""
    chart = fee_chart()
    drawn: list[tuple[float, float] | None] = []
    chart._redraw = lambda: drawn.append(chart._at)  # type: ignore[method-assign]

    chart._view = chart._view.panned(0.001, 0.0)
    chart._paint()
    for _ in range(4):
        await asyncio.sleep(0)

    assert len(drawn) == 1


async def test_a_burst_absorbed_mid_frame_costs_one_frame_not_five() -> None:
    """Whatever lands while a frame is in flight is absorbed into the cursor,
    and the loop draws where it got to -- once, not once per event."""
    chart = fee_chart()
    drawn: list[tuple[float, float] | None] = []
    chart._redraw = lambda: drawn.append(chart._at)  # type: ignore[method-assign]
    held = asyncio.Event()

    async def slow() -> None:
        await held.wait()

    async def painting() -> None:
        try:
            while chart._owed or chart._at != chart._drawn:
                chart._owed, chart._drawn = False, chart._at
                chart._redraw()
                await slow()
        finally:
            chart._painter = None

    chart._painting = painting  # type: ignore[method-assign]

    chart._hovered(Move(300.0, 120.0))
    for _ in range(3):
        await asyncio.sleep(0)
    assert drawn == [(300.0, 120.0)]

    for x in (310.0, 320.0, 330.0, 340.0):
        chart._hovered(Move(x, 120.0))
    held.set()
    for _ in range(4):
        await asyncio.sleep(0)

    assert drawn == [(300.0, 120.0), (340.0, 120.0)]


def test_a_cheap_frame_lets_the_cursor_stream_at_full_rate() -> None:
    chart = fee_chart()
    chart._cost = 0.004
    chart._pace()

    assert chart._gestures.hover_interval == HOVER_FAST


def test_an_expensive_frame_widens_the_clients_timer() -> None:
    """The only backpressure there is: the client sends on its own timer
    however slow the frames are, and those events queue where Python cannot
    reach them."""
    chart = fee_chart()
    chart._cost = 0.120
    chart._pace()

    assert chart._gestures.hover_interval == 120


def test_and_never_past_the_point_of_feeling_broken() -> None:
    chart = fee_chart()
    chart._cost = 10.0
    chart._pace()

    assert chart._gestures.hover_interval == HOVER_SLOW


def test_a_small_wobble_does_not_churn_the_control() -> None:
    """Every change is a patch to the client; a frame that wanders by a
    millisecond must not send one."""
    chart = fee_chart()
    chart._cost = 0.120
    chart._pace()
    chart._cost = 0.126
    chart._pace()

    assert chart._gestures.hover_interval == 120


async def test_the_pace_follows_what_frames_actually_cost() -> None:
    """Measured around the draw, so it tracks the client rather than a guess."""
    chart = fee_chart()
    chart._redraw = lambda: None  # type: ignore[method-assign]

    async def slow() -> None:
        await asyncio.sleep(0.05)

    chart._taken = slow  # type: ignore[method-assign]
    chart._hovered(Move(300.0, 120.0))
    for _ in range(6):
        await asyncio.sleep(0.02)

    assert chart._cost > 0.004


def test_a_travelling_pointer_draws_the_line_and_nothing_else() -> None:
    """While it is going somewhere, the band is not what is wanted and it is
    the whole cost of the frame."""
    chart = fee_chart()
    chart._settled = False

    assert chart._over(chart._profile, (500.0, 120.0)) == [chart._cursor]


def test_the_line_is_moved_rather_than_made_again() -> None:
    """Flet re-sends a shape it has not seen and skips one it has, so the
    travelling frame is two numbers on the wire."""
    chart = fee_chart()
    chart._settled = False
    first = chart._over(chart._profile, (500.0, 120.0))[-1]
    second = chart._over(chart._profile, (600.0, 120.0))[-1]

    assert first is second is chart._cursor
    assert chart._cursor.x1 == chart._cursor.x2 == 600.0


def test_the_curve_is_not_resent_while_only_the_pointer_moves() -> None:
    """The whole point of the second canvas: a patch names the canvas it is
    for, so the curve's shapes are not walked to find nothing changed."""
    chart = fee_chart()
    chart._redraw()
    curve = chart._canvas.shapes

    chart._at = (600.0, 120.0)
    chart._redraw()

    assert chart._canvas.shapes is curve
    assert chart._overlay.shapes == [chart._cursor]


def test_but_a_pan_does_resend_it() -> None:
    chart = fee_chart()
    chart._redraw()
    curve = chart._canvas.shapes

    chart._view = chart._view.panned(0.001, 0.0)
    chart._redraw()

    assert chart._canvas.shapes is not curve


def test_and_the_band_arrives_once_it_stops() -> None:
    chart = fee_chart()
    chart._settled = True
    over = chart._over(chart._profile, (500.0, 120.0))

    assert len(over) > 1
    assert over[-1] is chart._cursor


def test_the_line_snaps_onto_the_sample_when_the_band_is_drawn() -> None:
    """Otherwise the line, the dot and the band disagree by half a sample."""
    chart = fee_chart()
    chart._settled = True
    chart._over(chart._profile, (500.0, 120.0))
    loose = chart._cursor.x1

    assert loose != 500.0
    assert abs(loose - 500.0) < 20.0


async def test_the_band_follows_the_pointer_coming_to_rest() -> None:
    chart = fee_chart()
    chart._hovered(Move(500.0, 120.0))
    assert chart._settled is False

    await asyncio.sleep(SETTLE * 2.5)
    assert chart._settled is True


async def test_moving_again_takes_the_band_off_the_old_price() -> None:
    chart = fee_chart()
    chart._hovered(Move(500.0, 120.0))
    await asyncio.sleep(SETTLE * 2.5)
    assert chart._settled is True

    chart._hovered(Move(560.0, 120.0))
    assert chart._settled is False


def test_a_finger_gets_the_band_at_once() -> None:
    """There is no travel to draw on a touch screen: it arrives where it
    means to, so waiting for it to hold still would only be a delay."""
    chart = fee_chart()
    chart._hovered(Move(500.0, 120.0, touch=True))

    assert chart._settled is True
    assert chart._settler is None
