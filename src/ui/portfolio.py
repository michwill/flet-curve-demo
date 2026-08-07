"""The portfolio page: which pools this address is in, and for how much.

Nobody publishes that list, so it is read from the chain -- see
`curve.portfolio` for the scan and what it costs. What matters here is
that the scan takes a second or two, and a second or two of blank page
reads as a broken one. So the page has three states and moves through
them in order:

  * **remembered** -- what the last visit found, drawn immediately and
    marked as such. Usually right, and right *now*;
  * **refreshed** -- those same pools re-read, which is a handful of
    calls. The numbers on screen become current before the pool list has
    finished loading;
  * **scanned** -- everything else, which is where a new position turns
    up. A progress bar counts this one, because it is the long part.

The table is sorted by what a position is worth, which is the order the
question is usually asked in.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from curve.format import compact_usd, percent, short_address, token_amount
from curve.portfolio import LP_UNIT, Holding

from . import safe_update, theme
from .logos import initials_mark
from .typography import BODY, LABEL, METRIC, ROW_TITLE, SMALL

#: Column widths, shared by the header and the rows so they line up.
W_WALLET = 150
W_STAKED = 150
W_SHARE = 110
W_VALUE = 130

#: Below this a position is dust -- a wei or two left behind by a
#: withdrawal, which nearly every long-lived address has several of. They
#: are still listed, because "why is this here" is a fair question and
#: hiding it does not answer it, but they are drawn quietly.
DUST_USD = 0.01


class HoldingRow(ft.Container):
    """One position. Click it to open the pool."""

    def __init__(
        self,
        holding: Holding,
        on_open: Callable[[Holding], None],
        index: int = 0,
        narrow: bool = False,
    ) -> None:
        self.holding = holding
        quiet = holding.value < DUST_USD
        super().__init__(
            content=self._card(holding, quiet) if narrow else self._row(holding, quiet),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            on_click=lambda _e: on_open(holding),
            ink=True,
            key=f"holding-row-{index}",
        )

    def _name(self, holding: Holding) -> ft.Control:
        return ft.Row(
            [
                ft.Row(
                    [initials_mark(symbol, 22) for symbol in holding.symbols[:4]],
                    spacing=-6,
                    tight=True,
                ),
                ft.Column(
                    [
                        ft.Text(holding.name or short_address(holding.address),
                                size=ROW_TITLE, weight=ft.FontWeight.W_500),
                        ft.Text(" ".join(holding.symbols), size=SMALL,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                    spacing=1,
                    expand=True,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )

    def _row(self, holding: Holding, quiet: bool) -> ft.Control:
        colour = ft.Colors.ON_SURFACE_VARIANT if quiet else None
        return ft.Row(
            [
                self._name(holding),
                _cell(_lp(holding.wallet), W_WALLET, colour),
                _cell(_lp(holding.staked) if holding.gauge else "-", W_STAKED, colour),
                _cell(percent(holding.share * 100, places=3), W_SHARE, colour),
                _cell(compact_usd(holding.value), W_VALUE, colour, weight=ft.FontWeight.W_500),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _card(self, holding: Holding, quiet: bool) -> ft.Control:
        """Two lines instead of five columns, as the pool list does."""
        return ft.Column(
            [
                ft.Row(
                    [
                        self._name(holding),
                        ft.Text(compact_usd(holding.value), size=BODY,
                                weight=ft.FontWeight.W_500),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [
                        _metric("In wallet", _lp(holding.wallet)),
                        _metric("Staked", _lp(holding.staked)) if holding.gauge else ft.Container(),
                        _metric("Share", percent(holding.share * 100, places=3)),
                    ],
                    spacing=14,
                    wrap=True,
                ),
            ],
            spacing=6,
        )


#: Below this an LP balance is shown as "< 0.0001" rather than rounded.
#: A wei left behind by a withdrawal is not zero, and printing it as zero
#: raises exactly the question the row exists to answer.
DUST_LP = 1e-4


def _lp(amount: int) -> str:
    """An LP balance, in whole tokens."""
    if not amount:
        return "0"
    whole = amount / LP_UNIT
    if 0 < whole < DUST_LP:
        return f"< {DUST_LP:g}"
    return token_amount(whole)


def _cell(value: str, width: float, colour: str | None, weight=None) -> ft.Control:
    return ft.Container(
        ft.Text(value, size=BODY, color=colour, weight=weight,
                text_align=ft.TextAlign.RIGHT),
        width=width,
        alignment=ft.Alignment.CENTER_RIGHT,
    )


def _metric(label: str, value: str) -> ft.Control:
    return ft.Row(
        [
            ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(value, size=SMALL),
        ],
        spacing=4,
        tight=True,
    )


class PortfolioView(ft.Column):
    """The page. Owns its rows; the scan itself is the app's job."""

    def __init__(
        self,
        page: ft.Page,
        on_open: Callable[[Holding], None],
        narrow: bool = False,
    ) -> None:
        self._page = page
        self._on_open = on_open
        self._narrow = narrow
        self._holdings: list[Holding] = []

        self.account = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.total = ft.Text("", size=METRIC, weight=ft.FontWeight.BOLD)
        self.status = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.progress = ft.ProgressBar(visible=False, height=3)

        self.rows = ft.ListView(expand=True, spacing=0, key="holding-rows")
        self._header = self._build_header()
        self._rows_box = ft.Container(self.rows, expand=True, theme=theme.rows_theme(page))
        self._table = ft.Container(
            ft.Column([self._header, self._rows_box], spacing=0, expand=True),
            bgcolor=ft.Colors.SURFACE,
            border=theme.panel_border(page),
            border_radius=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            expand=True,
            shadow=theme.panel_shadow(page),
        )
        self.empty = ft.Container(
            ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
            padding=ft.Padding.symmetric(vertical=24, horizontal=16),
            visible=False,
        )

        super().__init__(
            controls=[
                ft.Row(
                    [
                        ft.Column(
                            [ft.Text("PORTFOLIO", size=LABEL, weight=ft.FontWeight.BOLD),
                             self.account],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Column(
                            [ft.Text("Total value", size=LABEL,
                                     color=ft.Colors.ON_SURFACE_VARIANT), self.total],
                            spacing=0,
                            horizontal_alignment=ft.CrossAxisAlignment.END,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.END,
                ),
                self.progress,
                self.status,
                self.empty,
                self._table,
            ],
            spacing=10,
            expand=True,
        )

    # -- what the app calls ------------------------------------------------

    def set_account(self, account: str) -> None:
        self.account.value = short_address(account) if account else ""
        safe_update(self.account)

    def show(self, holdings: list[Holding], *, note: str = "") -> None:
        """Draw a set of positions, with a line saying how fresh it is."""
        self._holdings = holdings
        self.rows.controls = [
            HoldingRow(holding, self._on_open, index, self._narrow)
            for index, holding in enumerate(holdings)
        ]
        self.total.value = compact_usd(sum(holding.value for holding in holdings))
        self.status.value = note
        self.status.visible = bool(note)
        self._table.visible = bool(holdings)
        safe_update(self)

    def say(self, message: str) -> None:
        """The page has nothing to show, and this is why."""
        self.rows.controls = []
        self.total.value = ""
        self.status.value = ""
        self.status.visible = False
        self._table.visible = False
        self.empty.content = ft.Text(message, size=SMALL,
                                     color=ft.Colors.ON_SURFACE_VARIANT)
        self.empty.visible = True
        safe_update(self)

    def scanning(self, done: int, total: int) -> None:
        """Progress, in calls. A scan is over a thousand of them."""
        self.empty.visible = False
        self.progress.visible = done < total
        self.progress.value = (done / total) if total else None
        self.status.value = (
            f"Checking {total:,} balances… {done * 100 // max(total, 1)}%"
            if done < total
            else ""
        )
        self.status.visible = done < total
        safe_update(self)

    def done_scanning(self) -> None:
        self.progress.visible = False
        safe_update(self.progress)

    def set_layout(self, narrow: bool) -> None:
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._header.visible = not narrow
        self.show(self._holdings, note=self.status.value or "")

    def rebuild(self) -> None:
        """Take on a theme that changed -- see `ui.theme`."""
        self._table.shadow = theme.panel_shadow(self._page)
        self._table.border = theme.panel_border(self._page)
        self._header.bgcolor = theme.header_bg(self._page)
        self._rows_box.theme = theme.rows_theme(self._page)
        safe_update(self)

    # -- header ------------------------------------------------------------

    def _build_header(self) -> ft.Container:
        return ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Text("Pool", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
                        expand=True,
                    ),
                    _head("In wallet", W_WALLET),
                    _head("Staked", W_STAKED),
                    _head("Share of pool", W_SHARE),
                    _head("Value", W_VALUE),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=8),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE)),
            bgcolor=theme.header_bg(self._page),
            visible=not self._narrow,
        )


def _head(label: str, width: float) -> ft.Control:
    return ft.Container(
        ft.Text(label, size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.RIGHT),
        width=width,
        alignment=ft.Alignment.CENTER_RIGHT,
    )
