"""The depth chart's axis, which is logarithmic and has to stay readable."""

from __future__ import annotations

import math
from itertools import pairwise

from curve.liquidity import Profile, Sample
from ui.depthchart import PRICE_LABELS, DepthChart, price_text, price_ticks

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
