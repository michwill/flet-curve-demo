"""Sizing a box to text nobody can measure until it is drawn."""

from __future__ import annotations

from ui.typography import text_width


def test_a_longer_string_is_wider():
    assert text_width("LP token", 14) < text_width("Depth: WBTC / crvUSD", 14)


def test_width_scales_with_the_font():
    assert text_width("USDC", 28) == 2 * text_width("USDC", 14)


def test_narrow_glyphs_cost_less_than_wide_ones():
    """`lll` and `WWW` are the same three characters and nothing like the
    same width, which is the whole reason this is not a character count."""
    assert text_width("lll", 14) < text_width("nnn", 14) < text_width("WWW", 14)


def test_nothing_is_no_width():
    assert text_width("", 14) == 0.0


def test_the_estimate_is_in_the_right_neighbourhood():
    """Roboto at 14px puts `Depth: scrvUSD / PYUSD` near 160px.  Pinned loosely
    -- what matters is that it is not out by a factor.
    """
    assert 120 < text_width("Depth: scrvUSD / PYUSD", 14) < 220
