"""What a transaction is doing, said the same way everywhere.

A transaction takes two waits the user cannot see into: the wallet's
prompt, and then twelve seconds of block. A line of text that simply sits
there is indistinguishable from one that has stopped, so the app says
*which* of the two it is waiting on and keeps a spinner turning while it
does -- and, once it is over, says whether it worked in a colour that can
be read before the words are.

That was written for the pool's action panels and lived inside them. It
belongs to the app: the portfolio claims from the same wallet, against the
same chain, with the same two waits, and a claim that reported itself in
plain grey text looked like a different program's idea of what a pending
transaction is.

Three states, and the tint follows the text colour rather than being
passed alongside it -- so a caller cannot say "green" and get an amber
panel:

  * **pending** -- a spinner and the primary colour, for anything still
    in flight;
  * **failed** -- the error colour, for a chain or a wallet saying no;
  * **done** -- green, for a receipt.

Anything else is a neutral remark and gets the quietest tint of the four.
"""

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
    """A tinted line that says what is happening, or nothing at all.

    Invisible while empty, because Flet skips an invisible control
    entirely: a panel that is merely blank still takes its share of the
    column's spacing, and every one of these sits in a column that has
    something under it.

    The text is selectable. Half of what appears here is a transaction
    hash, and a hash that cannot be copied is a hash that has to be
    retyped.
    """

    def __init__(self, page: ft.Page) -> None:
        self._page = page
        self.text = ft.Text("", size=SMALL, selectable=True, expand=True)
        #: A wallet takes seconds and a block takes twelve.
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
        # Rebuilt rather than fixed at construction: a panel outlives any
        # number of theme switches, and `theme.panel_shadow` reads the
        # live page. Same rule as `buttons.Shadowed`.
        self.shadow = theme.panel_shadow(self._page, inset=True)
