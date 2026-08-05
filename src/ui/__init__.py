"""Flet controls. Everything in here imports `curve`, never the reverse.

That direction is what keeps the logic layer testable without a running
app: `curve/` knows nothing about Flet, so its sorting, formatting, ABI
encoding and API parsing can be exercised by plain pytest.
"""

from __future__ import annotations


def safe_update(control) -> None:
    """`update()` the control if it is on a page; otherwise do nothing.

    Flet's `BaseControl.page` *raises* `RuntimeError` when the control has
    not been added to a page yet, rather than returning None -- so the
    obvious `if self.page: self.update()` raises the very error it looks
    like it is guarding against. There is no public `is_mounted`, so the
    honest spelling is to attempt the update and swallow that one case.

    Populating a view before it is mounted is normal: the app builds the
    detail page and only then swaps it into the layout, and the tests build
    views with no page at all.
    """
    try:
        control.update()
    except RuntimeError:
        pass
