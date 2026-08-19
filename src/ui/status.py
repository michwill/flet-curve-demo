"""What a transaction is doing, said the same way everywhere."""

from __future__ import annotations

import flet as ft

from . import theme
from .typography import SMALL

#: Green is not in Material's scheme -- there is no `ft.Colors.SUCCESS` --
#: so the one this app uses is named here rather than spelled out at each
#: call site, which is how the tint and the text came to disagree.
DONE = ft.Colors.GREEN_600
FAILED = ft.Colors.ERROR


def tint(colour: str | None, pending: bool) -> str:
    """The panel behind a status line, keyed to what it is saying."""
    if pending:
        return ft.Colors.with_opacity(0.10, ft.Colors.PRIMARY)
    if colour == FAILED:
        return ft.Colors.with_opacity(0.10, ft.Colors.ERROR)
    if colour == DONE:
        return ft.Colors.with_opacity(0.12, DONE)
    return ft.Colors.with_opacity(0.06, ft.Colors.ON_SURFACE)


class StatusPanel(ft.Container):
    """A tinted line that says what is happening, or nothing at all."""

    def __init__(self, page: ft.Page) -> None:
        self._page = page
        self.text = ft.Text("", size=SMALL, selectable=True, expand=True)
        self.spinner = ft.ProgressRing(width=14, height=14, stroke_width=2)
        super().__init__(
            ft.Row([self.spinner, self.text], spacing=10, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=8,
            visible=False,
            shadow=theme.panel_shadow(page, inset=True),
        )

    def say(
        self, message: str, colour: str | None = None, *, pending: bool = False
    ) -> None:
        """Show a status. `pending` means a spinner and a neutral tint."""
        self.text.value = message
        self.text.color = colour or ft.Colors.ON_SURFACE_VARIANT
        self.spinner.visible = pending
        self.visible = bool(message)
        self.bgcolor = tint(colour, pending)

    def clear(self) -> None:
        self.say("")

    @property
    def busy(self) -> bool:
        """Is something in flight? What greys the buttons that start it."""
        return bool(self.visible and self.spinner.visible)

    def before_update(self) -> None:
        super().before_update()
        self.shadow = theme.panel_shadow(self._page, inset=True)
