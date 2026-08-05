"""Chart data preparation, and the crosshair.

`CandlestickChart` draws the candles, so what is left to test is what this
app decides: the fitted window, the axis ticks, and what the crosshair
draws. The pan/zoom arithmetic is in `test_viewport.py`.
"""

from __future__ import annotations

import math

import flet_charts as fc

from curve.api import Candle
from ui.candles import (
    DATE_LABELS,
    PRICE_LABELS,
    CandleChart,
    build_spots,
    crosshair_shapes,
    date_axis,
    fit,
    format_price,
    interval_decimals,
    nice_interval,
    price_axis,
    price_decimals,
)
from ui.viewport import Plot, Viewport

COLOURS = {"line_color": "#888", "text_color": "#000", "box_color": "#fff"}


def series(prices: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(time=1_700_000_000 + i * 3600, open=o, high=h, low=lo, close=c)
        for i, (o, h, lo, c) in enumerate(prices)
    ]


def flat(n: int = 20) -> list[Candle]:
    return series([(1.0, 1.2, 0.8, 1.1)] * n)


def plot() -> Plot:
    return Plot(800.0, 400.0)


# -- fitting ---------------------------------------------------------------


def test_fit_covers_every_candle_with_headroom() -> None:
    view = fit(series([(1.0, 2.0, 0.5, 1.5), (1.5, 1.8, 1.2, 1.3)]))
    assert view.y_min < 0.5
    assert view.y_max > 2.0
    assert view.x_min < 0
    assert view.x_max >= 1


def test_fit_on_a_flat_series_invents_a_range() -> None:
    """Pegged pairs really do produce identical OHLC across a window."""
    view = fit(series([(1.0, 1.0, 1.0, 1.0)] * 5))
    assert view.y_max > view.y_min


def test_fit_on_a_zero_priced_series_is_survivable() -> None:
    view = fit(series([(0.0, 0.0, 0.0, 0.0)]))
    assert view.y_max > view.y_min


def test_fit_on_an_empty_series_is_usable() -> None:
    view = fit([])
    assert view.y_max > view.y_min
    assert view.x_max > view.x_min


# -- axis precision --------------------------------------------------------


def test_axis_labels_are_all_distinct_on_a_tight_range() -> None:
    """A stable pool ranges over ~0.0003; four decimals prints "1.027" thrice."""
    labels = [
        lb.label.value for lb in price_axis(Viewport(0, 10, 1.0268, 1.0271)).labels
    ]
    assert len(set(labels)) == len(labels), labels


def test_axis_labels_are_distinct_across_very_different_scales() -> None:
    for lo, hi in ((1.0268, 1.0271), (0.9, 1.1), (1800.0, 2600.0), (1e-7, 3e-7)):
        labels = [lb.label.value for lb in price_axis(Viewport(0, 10, lo, hi)).labels]
        assert len(set(labels)) == len(labels), (lo, hi, labels)


def test_axis_labels_are_never_padded_with_dead_zeros() -> None:
    labels = [lb.label.value for lb in price_axis(Viewport(0, 10, 0.9, 1.1)).labels]
    assert "1.0" in labels and "1.000000" not in labels


def test_price_decimals_stays_within_sane_bounds() -> None:
    assert price_decimals(0) == 2
    assert 2 <= price_decimals(1e-12) <= 10
    assert price_decimals(5000.0) == 2


def test_interval_decimals_does_not_over_pad() -> None:
    assert interval_decimals(0.0001) == 4
    assert interval_decimals(0.1) == 1
    assert interval_decimals(250) == 0
    assert interval_decimals(2.5) == 1
    assert interval_decimals(0) == 2


def test_large_prices_are_grouped_not_padded() -> None:
    assert format_price(2431.5, 2) == "2,432"
    assert format_price(1.0268, 4) == "1.0268"
    assert format_price(0) == "0"


def test_nice_interval_picks_round_steps() -> None:
    for span, target in ((1.0, 4), (0.0003, 4), (800.0, 4), (0.2, 4)):
        step = nice_interval(span, target)
        mantissa = step / 10 ** math.floor(math.log10(step))
        assert round(mantissa, 6) in (1.0, 2.0, 2.5, 5.0), step


def test_nice_interval_survives_degenerate_input() -> None:
    assert nice_interval(0, 4) == 1.0
    assert nice_interval(1.0, 0) == 1.0


# -- axes ------------------------------------------------------------------


def test_price_axis_labels_sit_on_multiples_of_the_interval() -> None:
    """The chart ticks at multiples of `label_spacing`, counted from zero.

    A label at any other value is silently dropped -- which left the axis
    showing only its min and max.
    """
    axis = price_axis(Viewport(0, 10, 0.9, 1.1))
    assert axis.label_spacing > 0
    assert 2 <= len(axis.labels) <= PRICE_LABELS + 2
    for label in axis.labels:
        multiples = label.value / axis.label_spacing
        assert abs(multiples - round(multiples)) < 1e-6, label.value


def test_price_axis_labels_stay_inside_the_window() -> None:
    view = Viewport(0, 10, 0.9, 1.1)
    for label in price_axis(view).labels:
        assert view.y_min <= label.value <= view.y_max


def test_date_axis_thins_labels_so_they_cannot_collide() -> None:
    candles = flat(365)
    assert len(date_axis(candles, fit(candles)).labels) <= DATE_LABELS + 2


def test_date_axis_follows_the_window_when_zoomed() -> None:
    """Zoomed into a few candles, the labels must be those candles'."""
    candles = flat(300)
    zoomed = date_axis(candles, Viewport(100.0, 110.0, 1.0, 2.0))
    assert zoomed.labels, "a zoomed window should still be labelled"
    for label in zoomed.labels:
        assert 100 <= label.value <= 110


def test_date_axis_labels_index_real_candles() -> None:
    candles = flat(12)
    for label in date_axis(candles, fit(candles)).labels:
        assert 0 <= label.value < len(candles)


def test_date_axis_on_an_empty_series() -> None:
    assert date_axis([], fit([])).labels == []


# -- spots -----------------------------------------------------------------


def test_spots_carry_ohlc_and_are_indexed_from_zero() -> None:
    spots = build_spots(series([(1.0, 1.2, 0.8, 1.1), (1.1, 1.3, 1.0, 0.9)]))
    assert [s.x for s in spots] == [0, 1]
    assert isinstance(spots[0], fc.CandlestickChartSpot)
    assert (spots[0].open, spots[0].high, spots[0].low, spots[0].close) == (
        1.0,
        1.2,
        0.8,
        1.1,
    )


def test_spots_suppress_the_built_in_tooltip() -> None:
    """The crosshair readout replaces it; two tooltips would fight."""
    assert all(not s.show_tooltip for s in build_spots(flat(3)))


def test_build_spots_on_an_empty_series() -> None:
    assert build_spots([]) == []


# -- crosshair -------------------------------------------------------------


def test_crosshair_draws_two_lines_through_the_cursor() -> None:
    candles = flat(50)
    shapes = crosshair_shapes(plot(), fit(candles), candles, 400.0, 200.0, **COLOURS)
    lines = [s for s in shapes if type(s).__name__ == "Line"]
    assert len(lines) == 2
    vertical = next(s for s in lines if s.x1 == s.x2)
    horizontal = next(s for s in lines if s.y1 == s.y2)
    assert vertical.x1 == 400.0
    assert horizontal.y1 == 200.0


def test_crosshair_reads_out_the_price_under_the_cursor() -> None:
    candles = flat(50)
    view, p, py = fit(candles), plot(), 200.0
    shapes = crosshair_shapes(p, view, candles, 400.0, py, **COLOURS)
    texts = [s.value for s in shapes if type(s).__name__ == "Text"]
    assert format_price(p.data_y(py, view), price_decimals(view.y_span)) in texts


def test_crosshair_reads_out_the_time_and_ohlc_of_the_hovered_candle() -> None:
    candles = flat(50)
    shapes = crosshair_shapes(plot(), fit(candles), candles, 400.0, 200.0, **COLOURS)
    joined = " ".join(s.value for s in shapes if type(s).__name__ == "Text")
    assert ":" in joined  # an HH:MM stamp
    for marker in ("O ", "H ", "L ", "C "):
        assert marker in joined


def test_crosshair_is_empty_outside_the_plot_area() -> None:
    candles = flat(50)
    view, p = fit(candles), plot()
    assert crosshair_shapes(p, view, candles, 10.0, 200.0, **COLOURS) == []
    assert crosshair_shapes(p, view, candles, 400.0, 395.0, **COLOURS) == []


def test_crosshair_past_the_last_candle_still_shows_a_price() -> None:
    """Overscrolled past the data, the price line must still read out."""
    candles = flat(10)
    shapes = crosshair_shapes(
        plot(), Viewport(500.0, 600.0, 1.0, 2.0), candles, 400.0, 200.0, **COLOURS
    )
    assert shapes  # the lines and the price box
    joined = " ".join(s.value for s in shapes if type(s).__name__ == "Text")
    assert "O " not in joined  # but no candle to describe


# -- the control -----------------------------------------------------------


def test_chart_builds_and_shows_a_message_when_empty() -> None:
    chart = CandleChart()
    chart.set_candles([])
    assert chart._empty.visible
    assert not chart._chart.visible


def test_setting_candles_fits_the_window_and_populates_the_axes() -> None:
    chart = CandleChart()
    chart.set_candles(flat(8))
    assert not chart._empty.visible
    assert chart._chart.visible
    assert len(chart._chart.spots) == 8
    assert chart._chart.min_y < chart._chart.max_y
    assert chart._chart.min_x < chart._chart.max_x
    assert chart._chart.left_axis is not None
    assert chart._chart.bottom_axis is not None


def test_new_data_refits_rather_than_keeping_a_stale_zoom() -> None:
    chart = CandleChart()
    chart.set_candles(flat(100))
    chart._view = Viewport(10.0, 20.0, 1.0, 1.05)
    chart.set_candles(flat(30))
    assert chart._view.x_span > 20  # refitted to the new series


def test_double_tap_resets_the_view() -> None:
    chart = CandleChart()
    chart.set_candles(flat(100))
    fitted = chart._view
    chart._view = Viewport(10.0, 20.0, 1.0, 1.05)
    chart.reset_view()
    assert chart._view.x_span == fitted.x_span


def test_the_chart_has_no_animation_at_all() -> None:
    """Any tween makes direct manipulation trail the cursor.

    `AnimationValue` is `Union[bool, int, Animation]` -- not Optional -- and
    the field defaults to a 150ms tween, so passing `None` silently keeps
    that default rather than disabling it. Only an explicit zero Duration
    actually means none, and this is the guard against it drifting back.
    """
    animation = CandleChart()._chart.animation
    duration = animation.duration
    assert duration.days == 0
    assert duration.hours == 0
    assert duration.minutes == 0
    assert duration.seconds == 0
    assert duration.milliseconds == 0
    assert duration.microseconds == 0


def test_summary_reports_the_change_over_the_window() -> None:
    chart = CandleChart()
    chart.set_candles(series([(1.0, 1.0, 1.0, 1.0), (1.0, 1.1, 1.0, 1.1)]))
    assert "+10.00%" in chart.summary
    chart.set_candles(series([(1.0, 1.0, 1.0, 1.0), (1.0, 1.0, 0.9, 0.9)]))
    assert "-10.00%" in chart.summary


def test_summary_is_empty_without_enough_data() -> None:
    chart = CandleChart()
    chart.set_candles([])
    assert chart.summary == ""
    chart.set_candles(flat(1))
    assert chart.summary == ""


def test_summary_stays_ascii() -> None:
    """The web build's font renders arrows as tofu; see curve.format."""
    chart = CandleChart()
    chart.set_candles(series([(1.0, 1.0, 1.0, 1.0), (1.0, 1.1, 1.0, 1.1)]))
    assert chart.summary.isascii()
