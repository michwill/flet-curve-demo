"""Flet controls. Everything in here imports `curve`, never the reverse."""

from __future__ import annotations

import contextlib
from typing import Any

import flet as ft

#: An event from whatever control fired it.
AnyEvent = ft.Event[Any]


def safe_update(control) -> None:
    """`update()` the control if it is on a page; otherwise do nothing."""
    if _swapped_out(control):
        return
    with contextlib.suppress(RuntimeError):
        control.update()


def _swapped_out(control) -> bool:
    """Off the tree, though its old parent still leads `update()` to the page."""
    try:
        return control._i not in control.page.session.index
    except RuntimeError:
        return False  # never mounted: `update()` raises, and that is caught
