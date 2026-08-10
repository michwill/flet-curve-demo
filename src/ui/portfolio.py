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
    up. The strip under the top bar counts this one, because it is the
    long part -- the app's own bar, in the same place the pool list uses,
    so that nothing on the page moves while it fills.

The table is sorted by what a position is worth, which is the order the
question is usually asked in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import flet as ft

from curve.earnings import ClaimPlan, Earning, claim_plan
from curve.format import compact_usd, percent, short_address, token_amount
from curve.models import Coin
from curve.portfolio import LP_UNIT, Holding

from . import buttons, safe_update, theme
from .logos import coin_stack
from .status import StatusPanel
from .typography import BODY, LABEL, METRIC, ROW_TITLE, SMALL

#: The same size the pool list draws its coin marks at, because this is
#: the same kind of row and they sit in the same column.
LOGO_SIZE = 27

#: Column widths, shared by the header and the rows so they line up.
W_WALLET = 130
W_STAKED = 130
W_APR = 100
W_REWARDS = 130
W_VALUE = 120

#: `Share of pool` used to sit between Staked and Value. Two columns that
#: change what somebody does -- what the position earns, and what is
#: waiting to be claimed -- are worth more than one that does not, and at
#: 610px of fixed width there was no room to simply add them: the Pool
#: column is what pays, and it is the one that must stay readable.

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
        earning: Earning | None = None,
    ) -> None:
        self.holding = holding
        #: None until the earnings pass has run -- which is a third read
        #: after the scan, so the rows exist before it arrives. The two
        #: columns show an en dash rather than a zero until then: "not
        #: read yet" and "earns nothing" are different answers.
        self.earning = earning
        quiet = holding.value < DUST_USD
        colour = ft.Colors.ON_SURFACE_VARIANT if quiet else None
        # Held rather than built inline, because the earnings pass arrives
        # after this row is on screen and must *edit* it. Rebuilding the
        # table instead is what the note below warns about: Flet matches a
        # keyed control to its predecessor and freezes it, and a frozen row
        # keeps its old text and never paints its token images. That is
        # exactly what a fourth `show()` did -- the table went blank.
        self._apr = ft.Text(self._apr_text(), size=BODY, color=colour,
                            text_align=ft.TextAlign.RIGHT)
        self._rewards = ft.Text(self._rewards_text(), size=BODY, color=colour,
                                text_align=ft.TextAlign.RIGHT)
        super().__init__(
            content=self._card(holding, quiet) if narrow else self._row(holding, quiet),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            on_click=lambda _e: on_open(holding),
            ink=True,
            # **No key.** The page draws its rows three times over -- the
            # remembered set, then the refreshed one, then the scan -- and
            # a keyed control that a rebuild matches to its predecessor is
            # frozen by Flet. A frozen row keeps its size and its text and
            # its token images never paint: they are fetched, they arrive,
            # and nothing appears. Nothing needs to find these by key.
        )

    def _apr_text(self) -> str:
        """The rate this account gets, not the pool's headline.

        Boosted by its veCRV share and scaled by how much of the position
        is actually staked -- see `curve.earnings`. The boost is worth
        showing beside it: two accounts in the same pool can be 2.5x
        apart, and without it the number looks wrong rather than personal.
        """
        if self.earning is None:
            return "\u2013"
        apr = self.earning.user_apr
        if apr <= 0:
            return "\u2013"
        boost = self.earning.boost
        return f"{percent(apr)}" + (f"  {boost:.2f}x" if boost > 1.0 else "")

    def _rewards_text(self) -> str:
        """What is waiting in the gauge, in dollars.

        A value rather than a list of amounts: several tokens do not fit a
        column, and the amounts themselves are on the pool's own Claim
        tab. What this column is for is noticing there is something there.
        """
        if self.earning is None:
            return "\u2013"
        value = self.earning.claimable_value
        if value <= 0:
            return "\u2013" if not self.earning.rewards else "< $0.01"
        return compact_usd(value)

    def apply(self, earning: Earning | None) -> None:
        """Fill in the two columns on a row that is already drawn."""
        self.earning = earning
        self._apr.value = self._apr_text()
        self._rewards.value = self._rewards_text()

    def _name(self, holding: Holding) -> ft.Control:
        # Real token images, overlapped exactly as the pool list overlaps
        # them: `coin_stack` wants coins, which is why a holding carries
        # addresses and not only symbols.
        coins = [Coin(address, symbol, 18, index=n)
                 for n, (address, symbol) in enumerate(holding.coins)]
        return ft.Row(
            [
                coin_stack(coins, holding.chain, LOGO_SIZE),
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
                _wrap(self._apr, W_APR),
                _wrap(self._rewards, W_REWARDS),
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
                        _metric_control("APR", self._apr),
                        _metric_control("Rewards", self._rewards),
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


def _wrap(control: ft.Control, width: float) -> ft.Control:
    """A fixed-width, right-aligned cell around a control somebody kept."""
    return ft.Container(control, width=width, alignment=ft.Alignment.CENTER_RIGHT)


def _metric_control(label: str, control: ft.Control) -> ft.Control:
    return ft.Row(
        [ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT), control],
        spacing=4,
        tight=True,
    )


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
        on_claim: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self._page = page
        self._on_open = on_open
        self._narrow = narrow
        self._holdings: list[Holding] = []
        #: Keyed by pool address, lowercased. Arrives after the scan.
        self._earnings: dict[str, Earning] = {}
        #: What the two buttons would send. Empty until the earnings pass.
        self._plan = ClaimPlan()
        self._on_claim = on_claim

        self.total = ft.Text("", size=METRIC, weight=ft.FontWeight.BOLD)

        # Two buttons, because claiming is two contracts: CRV is minted by
        # the Minter and takes every gauge at once, while the incentive
        # tokens come out of each gauge separately. One button would have
        # to lie about one of them -- see `curve.earnings.ClaimPlan`.
        self.claim_crv = ft.Button(
            "Claim CRV", on_click=lambda _e: self._claim(True),
            visible=False, style=buttons.style(page),
        )
        self.claim_rewards = ft.Button(
            "Claim rewards", on_click=lambda _e: self._claim(False),
            visible=False, style=buttons.style(page),
        )
        self.accrued = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        #: The same panel the pool's Deposit and Withdraw tabs use -- see
        #: `ui.status`. A claim waits on the same wallet and the same
        #: block, and saying so in loose grey text made the portfolio look
        #: like a different program's idea of a pending transaction.
        self.status = StatusPanel(page)
        self.claim_status = self.status.text
        self._claim_bar = ft.Container(
            ft.Row(
                [
                    self.accrued,
                    # Wrapping, and with **no flex child**. Both halves of
                    # that matter and they pull against each other:
                    #
                    #   * `Wrap` cannot lay out a flex child, so an
                    #     `expand=True` spacer in here is a layout assertion
                    #     rather than a stretchy gap -- and it fails
                    #     silently, dropping the subtree and taking the
                    #     table and its header with it while Python logs
                    #     nothing. The page went blank for exactly as long
                    #     as this bar was visible;
                    #   * but dropping `wrap` to get rid of the spacer just
                    #     moves the problem to the other end: a label and
                    #     two buttons in a fixed row overflow a phone.
                    #
                    # So it wraps and the buttons sit next to the label
                    # rather than against the right edge. A gap that costs
                    # the whole page is not worth the alignment.
                    buttons.shadowed(self.claim_crv, page),
                    buttons.shadowed(self.claim_rewards, page),
                ],
                spacing=8,
                run_spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=True,
            ),
            visible=False,
        )

        # A Column, not a `ListView`: the window scrolls, and a list that
        # scrolls inside it would be a second scrollbar in the middle of
        # the page -- see `CurveApp.body`.
        self.rows = ft.Column(spacing=0, key="holding-rows")
        self._header = self._build_header()
        self._rows_box = ft.Container(self.rows, theme=theme.rows_theme(page))
        self._table = ft.Container(
            ft.Column([self._header, self._rows_box], spacing=0),
            bgcolor=ft.Colors.SURFACE,
            border=theme.panel_border(page),
            border_radius=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            shadow=theme.panel_shadow(page),
        )
        self.empty = ft.Container(
            ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
            padding=ft.Padding.symmetric(vertical=24, horizontal=16),
            visible=False,
        )

        super().__init__(
            controls=[
                # One line: the page on the left, what it comes to on the
                # right. The address is already in the top bar, so it is
                # not repeated here.
                ft.Row(
                    [
                        ft.Text("Portfolio", size=METRIC, weight=ft.FontWeight.BOLD,
                                expand=True),
                        ft.Text("Total value:", size=SMALL,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                        self.total,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
                self._claim_bar,
                self.status,
                self.empty,
                self._table,
            ],
            spacing=10,
        )

    # -- what the app calls ------------------------------------------------

    def show(self, holdings: list[Holding]) -> None:
        """Draw a set of positions."""
        self._holdings = holdings
        self.rows.controls = [
            HoldingRow(
                holding, self._on_open, index, self._narrow,
                self._earnings.get(holding.address.lower()),
            )
            for index, holding in enumerate(holdings)
        ]
        self.total.value = compact_usd(sum(holding.value for holding in holdings))
        self._table.visible = bool(holdings)
        self.empty.visible = False
        safe_update(self)

    def _claim(self, crv: bool) -> None:
        """Hand the click to the app, as a task.

        The handler sends transactions and waits for them, so it is a
        coroutine -- and a Flet click handler that returns one leaves it
        un-awaited, which is a claim that silently never happens.
        """
        if self._on_claim is not None:
            self._page.run_task(self._on_claim, crv)

    def show_earnings(self, earnings: list[Earning], chain_id: int = 0) -> None:
        """Fill in the APR and rewards columns, and offer the claims.

        A separate pass rather than part of `show`, because it is a third
        read that lands after the rows are already on screen -- and the
        rows must not wait for it. Until it arrives both columns show an
        en dash, which says "not read" where a zero would say "nothing".

        What each button offers comes from the *plan* rather than from the
        rewards, because those are two different questions. A gauge can
        owe CRV on a chain with no Minter deployed on it, and there the
        answer to "is any CRV owed" is yes while the answer to "can this
        button send anything" is no.
        """
        self._earnings = {e.pool.lower(): e for e in earnings}
        self._plan = claim_plan(chain_id, earnings)
        owed = sum(e.claimable_value for e in earnings)
        crv = bool(self._plan.crv)
        extras = bool(self._plan.extras)

        self.claim_crv.visible = crv
        self.claim_rewards.visible = extras
        self.claim_crv.content = self._crv_label(earnings, len(self._plan.crv))
        self.claim_rewards.content = self._rewards_label(earnings)
        self.accrued.value = (
            f"Unclaimed rewards: {compact_usd(owed)}" if owed > 0
            else "Unclaimed rewards" if crv or extras
            else ""
        )
        self._claim_bar.visible = crv or extras
        for row in self.rows.controls:
            if isinstance(row, HoldingRow):
                row.apply(self._earnings.get(row.holding.address.lower()))
        safe_update(self)

    @staticmethod
    def _crv_label(earnings: list[Earning], transactions: int = 1) -> str:
        """"Claim 1.23 CRV", and "(4 txs)" when it is not one transaction.

        A claim is not a choice between amounts, so the number is not
        needed to decide anything; it is there because a button that
        commits an address to a transaction should say what the
        transaction is for, and "some CRV" is not that.

        The count is there for a harder reason. `mint_many` takes eight
        gauges on Ethereum and thirty-two elsewhere, so a large portfolio
        cannot be claimed in one send -- and a wallet that asks to sign a
        second time, unannounced, looks like a wallet that has been asked
        to sign something twice.
        """
        # "Claim 0 CRV" is what this says a moment after a claim confirms,
        # and it is not a mistake in the reading: CRV accrues every block,
        # so by the time the mint is mined a few hundred wei are owed
        # again. Too little to print, and printing it as zero makes the
        # claim that just landed look like it did not. So the number is
        # dropped and the button goes back to naming the token.
        owed = token_amount(sum(e.crv_owed for e in earnings))
        label = "Claim CRV" if owed == "0" else f"Claim {owed} CRV"
        return label + (f" ({transactions} txs)" if transactions > 1 else "")

    @staticmethod
    def _rewards_label(earnings: list[Earning]) -> str:
        """"Claim rewards ($12.34)", or without the value if there is none.

        Several tokens do not fit a button, so the incentives are named by
        what they come to rather than by what they are -- the amounts
        themselves are on each pool's Claim tab.

        A reward the API never priced is worth $0 here while being worth
        something in fact, and "Claim rewards ($0)" reads as a button that
        does nothing. So an unpriced claim simply drops the parenthesis.
        """
        value = sum(e.extras_value for e in earnings)
        return "Claim rewards" + (f" ({compact_usd(value)})" if value > 0 else "")

    def claiming(self, message: str, colour: str | None = None) -> None:
        """Say what a claim is doing, and stop it being pressed twice.

        A message with no colour is something still in flight -- the
        wallet prompt, or a block -- which is what greys the buttons and
        turns the spinner. A colour means it is over, one way or the
        other, and the buttons come back.
        """
        pending = bool(message) and colour is None
        self.claim_crv.disabled = pending
        self.claim_rewards.disabled = pending
        self.status.say(message, colour, pending=pending)
        safe_update(self)

    def say(self, message: str) -> None:
        """The page has nothing to show, and this is why."""
        self.rows.controls = []
        self.total.value = ""
        self._claim_bar.visible = False
        self._table.visible = False
        self.empty.content = ft.Text(message, size=SMALL,
                                     color=ft.Colors.ON_SURFACE_VARIANT)
        self.empty.visible = True
        safe_update(self)

    def set_layout(self, narrow: bool) -> None:
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._header.visible = not narrow
        self.show(self._holdings)

    def rebuild(self) -> None:
        """Take on a theme that changed -- see `ui.theme`."""
        self._table.shadow = theme.panel_shadow(self._page)
        self._table.border = theme.panel_border(self._page)
        self._header.bgcolor = theme.header_bg(self._page)
        self._rows_box.theme = theme.rows_theme(self._page)
        # See `PoolListView.rebuild`: rows built before the nested theme
        # arrives lose their token marks, so they are built again.
        self.show(self._holdings)

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
                    _head("Your APR", W_APR),
                    _head("Rewards", W_REWARDS),
                    _head("Value", W_VALUE),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=2),
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
