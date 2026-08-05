"""Chart data preparation.

`CandlestickChart` handles the drawing, so what is left to test is what
this app decides: the visible range, how precise an axis label has to be,
and the tooltip text. All pure functions of numbers -- no display, no
canvas, no running app.
"""

from __future__ import annotations

import flet_charts as fc

from curve.api import Candle
from ui.candles import (
    DATE_LABELS,
    PRICE_LABELS,
    CandleChart,
    build_spots,
    date_axis,
    format_price,
    interval_decimals,
    nice_interval,
    price_axis,
    price_decimals,
    price_range,
    spot_tooltip,
)


def series(prices: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(time=1_700_000_000 + i * 86400, open=o, high=h, low=lo, close=c)
        for i, (o, h, lo, c) in enumerate(prices)
    ]


# -- range -----------------------------------------------------------------


def test_range_covers_every_candle_with_padding() -> None:
    candles = series([(1.0, 2.0, 0.5, 1.5), (1.5, 1.8, 1.2, 1.3)])
    low, high = price_range(candles)
    assert low < 0.5
    assert high > 2.0


def test_flat_series_gets_an_invented_range() -> None:
    """Pegged pairs really do produce identical OHLC across a window."""
    low, high = price_range(series([(1.0, 1.0, 1.0, 1.0)] * 5))
    assert high > low


def test_zero_priced_flat_series_is_survivable() -> None:
    low, high = price_range(series([(0.0, 0.0, 0.0, 0.0)]))
    assert high > low


def test_empty_series_has_a_usable_default_range() -> None:
    low, high = price_range([])
    assert high > low


# -- axis precision --------------------------------------------------------


def test_axis_labels_are_never_padded_with_dead_zeros() -> None:
    labels = [lb.label.value for lb in price_axis(series([(0.9, 1.1, 0.9, 1.1)])).labels]
    assert "1.0" in labels and "1.000000" not in labels


def test_axis_labels_are_all_distinct_on_a_tight_range() -> None:
    """A stable pool ranges over ~0.0003; four decimals prints "1.027" thrice.

    Exactly what the crvUSD/USDT chart did before the precision became a
    function of the span rather than the magnitude.
    """
    candles = series([(1.0268, 1.0271, 1.0268, 1.0270)] * 20)
    axis = price_axis(candles)
    labels = [label.label.value for label in axis.labels]
    assert len(set(labels)) == len(labels), labels


def test_axis_labels_are_distinct_across_very_different_scales() -> None:
    for lo, hi in ((1.0268, 1.0271), (0.9, 1.1), (1800.0, 2600.0), (1e-7, 3e-7)):
        labels = [lb.label.value for lb in price_axis(series([(lo, hi, lo, hi)])).labels]
        assert len(set(labels)) == len(labels), (lo, hi, labels)


def test_price_decimals_stays_within_sane_bounds() -> None:
    assert price_decimals(0) == 2
    assert 2 <= price_decimals(1e-12) <= 10
    assert price_decimals(5000.0) == 2


def test_large_prices_are_grouped_not_padded() -> None:
    assert format_price(2431.5, 2) == "2,432"
    assert format_price(1.0268, 4) == "1.0268"
    assert format_price(0) == "0"


# -- axes ------------------------------------------------------------------


def test_price_axis_labels_sit_on_multiples_of_the_interval() -> None:
    """The chart ticks at multiples of `label_spacing`, counted from zero.

    A label at any other value is silently dropped -- which left the axis
    showing only its min and max. The date axis never hit this because its
    values are integer multiples of the stride already.
    """
    axis = price_axis(series([(1.0, 1.1, 0.9, 1.05)] * 10))
    assert axis.label_spacing > 0
    assert 2 <= len(axis.labels) <= PRICE_LABELS + 2
    for label in axis.labels:
        multiples = label.value / axis.label_spacing
        assert abs(multiples - round(multiples)) < 1e-6, label.value


def test_price_axis_labels_stay_inside_the_visible_range() -> None:
    candles = series([(1.0, 1.1, 0.9, 1.05)] * 10)
    low, high = price_range(candles)
    for label in price_axis(candles).labels:
        assert low <= label.value <= high


def test_nice_interval_picks_round_steps() -> None:
    for span, target in ((1.0, 4), (0.0003, 4), (800.0, 4), (0.2, 4)):
        step = nice_interval(span, target)
        mantissa = step / 10 ** __import__("math").floor(__import__("math").log10(step))
        assert round(mantissa, 6) in (1.0, 2.0, 2.5, 5.0), step


def test_nice_interval_survives_degenerate_input() -> None:
    assert nice_interval(0, 4) == 1.0
    assert nice_interval(1.0, 0) == 1.0


def test_interval_decimals_does_not_over_pad() -> None:
    """Deriving precision from the span printed "1.026800" for 1.0268."""
    assert interval_decimals(0.0001) == 4
    assert interval_decimals(0.1) == 1
    assert interval_decimals(250) == 0
    assert interval_decimals(2.5) == 1
    assert interval_decimals(0) == 2


def test_date_axis_thins_labels_so_they_cannot_collide() -> None:
    """A year of daily candles must not print 365 dates."""
    axis = date_axis(series([(1.0, 1.1, 0.9, 1.05)] * 365))
    assert len(axis.labels) <= DATE_LABELS + 1


def test_date_axis_labels_index_real_candles() -> None:
    candles = series([(1.0, 1.1, 0.9, 1.05)] * 12)
    for label in date_axis(candles).labels:
        assert 0 <= label.value < len(candles)


def test_date_axis_on_an_empty_series() -> None:
    assert date_axis([]).labels == []


# -- spots -----------------------------------------------------------------


def test_spots_carry_ohlc_and_are_indexed_from_zero() -> None:
    candles = series([(1.0, 1.2, 0.8, 1.1), (1.1, 1.3, 1.0, 0.9)])
    spots = build_spots(candles)
    assert [s.x for s in spots] == [0, 1]
    assert isinstance(spots[0], fc.CandlestickChartSpot)
    assert (spots[0].open, spots[0].high, spots[0].low, spots[0].close) == (
        1.0,
        1.2,
        0.8,
        1.1,
    )


def test_every_spot_gets_a_tooltip() -> None:
    spots = build_spots(series([(1.0, 1.2, 0.8, 1.1)] * 3))
    assert all(s.tooltip for s in spots)


def test_tooltip_names_the_date_and_all_four_prices() -> None:
    text = spot_tooltip(Candle(1_700_000_000, 1.0, 1.2, 0.8, 1.1), 4)
    for marker in ("O ", "H ", "L ", "C "):
        assert marker in text
    assert "1.2000" in text


def test_build_spots_on_an_empty_series() -> None:
    assert build_spots([]) == []


# -- the control -----------------------------------------------------------


def test_chart_builds_and_shows_a_message_when_empty() -> None:
    chart = CandleChart()
    chart.set_candles([])
    assert chart._empty.visible
    assert not chart._chart.visible


def test_setting_candles_populates_the_chart_and_its_axes() -> None:
    chart = CandleChart()
    chart.set_candles(series([(1.0, 1.2, 0.8, 1.1)] * 8))
    assert not chart._empty.visible
    assert chart._chart.visible
    assert len(chart._chart.spots) == 8
    assert chart._chart.left_axis is not None
    assert chart._chart.bottom_axis is not None
    assert chart._chart.min_y < chart._chart.max_y


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
    chart.set_candles(series([(1.0, 1.1, 0.9, 1.05)]))
    assert chart.summary == ""


def test_summary_stays_ascii() -> None:
    """The web build's font renders arrows as tofu; see curve.format."""
    chart = CandleChart()
    chart.set_candles(series([(1.0, 1.0, 1.0, 1.0), (1.0, 1.1, 1.0, 1.1)]))
    assert chart.summary.isascii()
