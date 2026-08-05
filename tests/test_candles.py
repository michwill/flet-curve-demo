"""Chart layout maths.

`plot_geometry` is a pure function of numbers, so the awkward cases -- a
flat series, a single candle, a box too small to draw in -- can be pinned
down without a canvas or a running app. Importing `ui.candles` pulls in
Flet, but only to build shape objects; nothing here needs a display.
"""

from __future__ import annotations

from curve.api import Candle
from ui.candles import (
    PADDING,
    _axis_prices,
    _format_price,
    _price_decimals,
    build_shapes,
    plot_geometry,
)


def series(prices: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(time=1_700_000_000 + i * 86400, open=o, high=h, low=lo, close=c)
        for i, (o, h, lo, c) in enumerate(prices)
    ]


def test_higher_price_maps_to_a_smaller_y() -> None:
    """Screen coordinates grow downward; prices must not be drawn upside down."""
    candles = series([(1.0, 2.0, 0.5, 1.5)])
    geometry = plot_geometry(candles, 800, 400)
    assert geometry.y(2.0) < geometry.y(0.5)


def test_price_range_is_padded_so_extremes_are_not_flush() -> None:
    candles = series([(1.0, 2.0, 1.0, 1.5)])
    geometry = plot_geometry(candles, 800, 400)
    assert geometry.low < 1.0
    assert geometry.high > 2.0


def test_flat_series_does_not_divide_by_zero() -> None:
    """Pegged stable pairs really do produce identical OHLC across a window."""
    candles = series([(1.0, 1.0, 1.0, 1.0)] * 5)
    geometry = plot_geometry(candles, 800, 400)
    assert geometry.high > geometry.low
    assert 0 <= geometry.y(1.0) <= 400


def test_zero_priced_flat_series_is_survivable() -> None:
    candles = series([(0.0, 0.0, 0.0, 0.0)])
    geometry = plot_geometry(candles, 800, 400)
    assert geometry.high > geometry.low


def test_candles_are_spaced_across_the_plot_and_stay_inside_it() -> None:
    candles = series([(1.0, 1.1, 0.9, 1.05)] * 10)
    geometry = plot_geometry(candles, 800, 400)
    xs = [geometry.x(i) for i in range(10)]
    assert xs == sorted(xs)
    assert xs[0] >= PADDING
    assert xs[-1] <= PADDING + geometry.plot_width


def test_candle_width_never_goes_subpixel() -> None:
    """A year of hourly candles is thousands of bars in ~800px."""
    candles = series([(1.0, 1.1, 0.9, 1.05)] * 5000)
    geometry = plot_geometry(candles, 800, 400)
    assert geometry.candle_width >= 1.0


def test_tiny_box_does_not_produce_negative_dimensions() -> None:
    geometry = plot_geometry(series([(1.0, 1.1, 0.9, 1.0)]), 10, 10)
    assert geometry.plot_width >= 1.0
    assert geometry.plot_height >= 1.0


def test_empty_series_still_yields_axis_shapes_but_no_candles() -> None:
    shapes = build_shapes([], 800, 400, grid_color="#000", text_color="#000")
    assert shapes  # gridlines and price labels are still drawn
    assert plot_geometry([], 800, 400).plot_width > 0


def test_build_shapes_emits_wick_and_body_per_candle() -> None:
    candles = series([(1.0, 1.2, 0.8, 1.1), (1.1, 1.3, 1.0, 0.9)])
    shapes = build_shapes(candles, 800, 400, grid_color="#000", text_color="#000")
    rects = [s for s in shapes if type(s).__name__ == "Rect"]
    # one body per candle
    assert len(rects) == 2
    # every body has a visible height even when open == close
    assert all(r.height >= 1.0 for r in rects)


def test_doji_body_is_still_visible() -> None:
    """open == close would otherwise be a zero-height rectangle."""
    shapes = build_shapes(
        series([(1.0, 1.2, 0.8, 1.0)]), 800, 400, grid_color="#000", text_color="#000"
    )
    rects = [s for s in shapes if type(s).__name__ == "Rect"]
    assert rects[0].height >= 1.0


def test_rising_and_falling_candles_get_different_colours() -> None:
    rising = build_shapes(
        series([(1.0, 1.2, 0.9, 1.1)]), 800, 400, grid_color="#000", text_color="#000"
    )
    falling = build_shapes(
        series([(1.1, 1.2, 0.9, 1.0)]), 800, 400, grid_color="#000", text_color="#000"
    )
    rising_body = [s for s in rising if type(s).__name__ == "Rect"][0]
    falling_body = [s for s in falling if type(s).__name__ == "Rect"][0]
    assert rising_body.paint.color != falling_body.paint.color


def test_axis_labels_are_all_distinct_on_a_tight_range() -> None:
    """A stable pool ranges over ~0.0003; four decimals prints "1.027" thrice.

    This is exactly what the crvUSD/USDT chart did before the precision
    became a function of the span rather than the magnitude.
    """
    candles = series([(1.0268, 1.0271, 1.0268, 1.0270)] * 20)
    geometry = plot_geometry(candles, 800, 400)
    decimals = _price_decimals(geometry.high - geometry.low)
    labels = [_format_price(p, decimals) for p in _axis_prices(geometry)]
    assert len(set(labels)) == len(labels), labels


def test_axis_labels_are_distinct_across_very_different_scales() -> None:
    for lo, hi in ((1.0268, 1.0271), (0.9, 1.1), (1800.0, 2600.0), (1e-7, 3e-7)):
        candles = series([(lo, hi, lo, hi)])
        geometry = plot_geometry(candles, 800, 400)
        decimals = _price_decimals(geometry.high - geometry.low)
        labels = [_format_price(p, decimals) for p in _axis_prices(geometry)]
        assert len(set(labels)) == len(labels), (lo, hi, labels)


def test_price_decimals_stays_within_sane_bounds() -> None:
    assert _price_decimals(0) == 2
    assert 2 <= _price_decimals(1e-12) <= 10
    assert _price_decimals(5000.0) == 2


def test_candle_rising_flag() -> None:
    assert Candle(0, 1.0, 1.0, 1.0, 1.5).rising
    assert not Candle(0, 1.5, 1.5, 1.0, 1.0).rising
    # open == close counts as rising, matching the usual convention
    assert Candle(0, 1.0, 1.0, 1.0, 1.0).rising
