"""Flet controls. Everything in here imports `curve`, never the reverse.

That direction is what keeps the logic layer testable without a running
app: `curve/` knows nothing about Flet, so its sorting, formatting, ABI
encoding and API parsing can be exercised by plain pytest.
"""

from __future__ import annotations

import contextlib
from typing import Any

import flet as ft

#: An event from whatever control fired it.
#:
#: Flet's own `ft.ControlEvent` is `Event[BaseControl]` to a type checker
#: (`Any` only at runtime), and `Event` is invariant in that parameter --
#: so a handler annotated with it cannot be passed to
#: `TextField(on_change=…)`, which asks for `Event[TextField]`. Naming the
#: concrete control instead is no good either: several handlers here are
#: shared between a TextField, a Dropdown and a RadioGroup. `Any` is the
#: accurate description of what they accept, and it is what Flet's alias
#: resolves to at runtime anyway.
AnyEvent = ft.Event[Any]


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
    with contextlib.suppress(RuntimeError):
        control.update()
