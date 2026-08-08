"""Measuring how tall text is, rather than assuming it.

One number in this app is a guess at a height in pixels -- how tall the
action panel has to be, because `TabBarView` cannot size to its content --
and it no longer scrolls when the guess comes up short. It clips, and what
clips is the bottom of the panel, which is the button you came to press.

Every other absolute size here is a width or a gap, and those look wrong
when they are wrong. This one stops working. So it is scaled by a line of
real text, measured on whatever platform the app is actually running on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ui import metrics


@pytest.fixture(autouse=True)
def unscaled():
    """Module state, shared by every test in the process."""
    metrics.reset()
    yield
    metrics.reset()


def report(height: float) -> None:
    """What Flet hands the handler: a size event carrying the height."""
    metrics._measured(SimpleNamespace(height=height, width=100))


def test_a_platform_that_matches_changes_nothing() -> None:
    report(metrics.LINE_HEIGHT)
    assert metrics.scale() == 1.0
    assert metrics.scaled(400) == 400


def test_taller_text_makes_the_panel_taller_by_as_much() -> None:
    """A 1.3 text scale grows the panel's content by about a third, so the
    panel grows by a third. Guessing a fixed margin instead is what leaves
    a Deposit button off the bottom of a phone."""
    report(metrics.LINE_HEIGHT * 1.3)
    assert metrics.scale() == pytest.approx(1.3)
    assert metrics.scaled(400) == pytest.approx(520)


def test_the_action_panel_follows_it() -> None:
    from ui.pool_detail import actions_height

    from .test_views import make_pool

    pool = make_pool(3)
    plain = actions_height(pool)
    report(metrics.LINE_HEIGHT * 1.5)

    assert actions_height(pool) == pytest.approx(plain * 1.5)


@pytest.mark.parametrize("height", [0, -20, None, "20", 20 * 4, 1])
def test_a_report_that_is_not_a_text_height_is_ignored(height) -> None:
    """Zero and None are what a control reports before layout settles, and
    scaling a panel by either is worse than not scaling it at all. The
    wild ones are the same thing arriving late."""
    report(height)
    assert metrics.scale() == 1.0


def test_the_probe_is_drawn_rather_than_hidden() -> None:
    """Flet does not build a control that is not visible, and one that is
    not built is never measured. Zero opacity is drawn."""
    control = metrics.probe()

    assert control.visible is not False
    assert control.opacity == 0.0
    assert control.on_size_change is not None
