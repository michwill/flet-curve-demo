"""The depth chart's axis, which is logarithmic and has to stay readable."""

from __future__ import annotations

import math
from itertools import pairwise

from ui.depthchart import PRICE_LABELS, price_text, price_ticks

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
