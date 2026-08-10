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
from curve.rewards import REWARDS

from . import buttons, pool_list, safe_update, theme
from .logos import coin_stack
from .pool_list import reward_line
from .responsive import Layout
from .status import StatusPanel
from .typography import BODY, LABEL, METRIC, ROW_TITLE, SMALL

#: The same size the pool list draws its coin marks at, because this is
#: the same kind of row and they sit in the same column.
LOGO_SIZE = 27

#: Column widths, taken from the pool list rather than chosen again here.
#: The two tables sit one click apart and hold the same kinds of thing --
#: a rate, a dollar figure, a stack of reward lines -- and columns that
#: nearly line up read worse than columns that plainly do not.
#:
#: `Your APR` gets the width the pool list gives *its* reward column,
#: because it now holds the same content: one marked line per token.
W_WALLET = pool_list.W_TVL
W_STAKED = pool_list.W_TVL
W_APR = pool_list.W_REWARDS
W_REWARDS = pool_list.W_VOLUME
W_VALUE = pool_list.W_TVL

#: `Share of pool` used to sit between Staked and Value. Two columns that
#: change what somebody does -- what the position earns, and what is
#: waiting to be claimed -- are worth more than one that does not, and at
#: 610px of fixed width there was no room to simply add them: the Pool
#: column is what pays, and it is the one that must stay readable.

#: Every column, widest layout first, in the order they are drawn.
COLUMNS = ("wallet", "staked", "apr", "rewards", "value")

#: What survives between the card breakpoint and the compact one. `In
#: wallet` goes, on the pool list's own rule -- the least decisive column
#: first. It reads zero for any position that is fully staked, which is
#: most of them, and the card layout below still shows it.
COMPACT_COLUMNS = ("staked", "apr", "rewards", "value")

#: Heading, and what to sort by. Descending always, like the pool list:
#: nobody opens a portfolio to find their smallest position.
SORTS: dict[str, tuple[str, Callable[[Holding, Earning | None], float]]] = {
    "wallet": ("In wallet", lambda h, _e: h.wallet),
    "staked": ("Staked", lambda h, _e: h.staked),
    "apr": ("Your APR", lambda _h, e: e.user_apr if e else -1.0),
    "rewards": ("Rewards", lambda _h, e: e.claimable_value if e else -1.0),
    "value": ("Value", lambda h, _e: h.value),
}

#: What the page opens on: what a position is worth, which is the order
#: the question is usually asked in.
DEFAULT_SORT = "value"

#: Sorts whose key comes from the earnings pass rather than from the scan.
#: Ordering by one of these has to wait for that read and be redone when
#: it lands -- the other three are known as soon as the rows are.
EARNED_SORTS = ("apr", "rewards")

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
        crv: str = "",
        columns: tuple[str, ...] = COLUMNS,
    ) -> None:
        self.holding = holding
        self._columns = columns
        #: None until the earnings pass has run -- which is a third read
        #: after the scan, so the rows exist before it arrives. The two
        #: columns show an en dash rather than a zero until then: "not
        #: read yet" and "earns nothing" are different answers.
        self.earning = earning
        #: CRV's address on this chain, for its mark. Empty is an answer:
        #: a chain with no CRV deployment gets the rate without a logo.
        self._crv = crv
        quiet = holding.value < DUST_USD
        colour = ft.Colors.ON_SURFACE_VARIANT if quiet else None
        # Held rather than built inline, because the earnings pass arrives
        # after this row is on screen and must *edit* it. Rebuilding the
        # table instead is what the note below warns about: Flet matches a
        # keyed control to its predecessor and freezes it, and a frozen row
        # keeps its old text and never paints its token images. That is
        # exactly what a fourth `show()` did -- the table went blank.
        #
        # A column, because a gauge paying CRV and two incentive tokens has
        # three rates and they are three different reasons to be in the
        # pool. Same shape the pool list uses for the same facts.
        self._apr = ft.Column(
            self._apr_lines(colour),
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.END,
        )
        self._rewards = ft.Text(self._rewards_text(), size=BODY, color=colour,
                                text_align=ft.TextAlign.RIGHT)
        self._quiet_colour = colour
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

    def _apr_lines(self, colour: str | None) -> list[ft.Control]:
        """The rates this account gets, one token per line, each marked.

        Not the pool's headline: boosted by this address's veCRV share and
        scaled by how much of the position is actually staked -- see
        `curve.earnings`. The boost itself is not printed, being an input
        to these numbers rather than another of them.

        Split by token rather than summed, because the sum answers a
        question nobody has. A position paying 6% in CRV and 2% in ARB is
        a different position from one paying 8% of either -- one is a bet
        on emissions and the other on a campaign that ends -- and the mark
        beside each rate is what says which.
        """
        if self.earning is None:
            return [_apr_text("\u2013", colour)]
        lines: list[ft.Control] = []
        crv = self.earning.user_crv_apr
        if crv > 0:
            lines.append(reward_line(percent(crv), self._crv, "CRV",
                                     self.holding.chain, muted=colour is not None))
        for incentive, rate in self.earning.user_incentives():
            lines.append(reward_line(percent(rate), incentive.token_address,
                                     incentive.symbol, self.holding.chain, muted=True))
        return lines or [_apr_text("\u2013", colour)]

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

    def apply(self, earning: Earning | None, crv: str = "") -> None:
        """Fill in the two columns on a row that is already drawn."""
        self.earning = earning
        self._crv = crv
        self._apr.controls = self._apr_lines(self._quiet_colour)
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
        cells = {
            "wallet": lambda: _cell(_lp(holding.wallet), W_WALLET, colour),
            "staked": lambda: _cell(
                _lp(holding.staked) if holding.gauge else "-", W_STAKED, colour
            ),
            "apr": lambda: _wrap(self._apr, W_APR),
            "rewards": lambda: _wrap(self._rewards, W_REWARDS),
            "value": lambda: _cell(compact_usd(holding.value), W_VALUE, colour,
                                   weight=ft.FontWeight.W_500),
        }
        return ft.Row(
            [self._name(holding)] + [cells[key]() for key in self._columns],
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


def _apr_text(value: str, colour: str | None) -> ft.Control:
    """The column with no rate to break down: an en dash, or nothing."""
    return ft.Text(value, size=BODY, color=colour, text_align=ft.TextAlign.RIGHT)


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
        #: Which column the table is ordered by, and which are drawn.
        self._sort = DEFAULT_SORT
        self._columns: tuple[str, ...] = COMPACT_COLUMNS if narrow else COLUMNS
        #: What the two buttons would send. Empty until the earnings pass.
        self._plan = ClaimPlan()
        #: CRV's address on the chain being shown, for the mark beside its
        #: rate. Known only once the earnings pass names the chain.
        self._crv = ""
        self._on_claim = on_claim

        self.total = ft.Text("", size=METRIC, weight=ft.FontWeight.BOLD)

        # Two buttons, because claiming is two contracts: CRV is minted by
        # the Minter and takes every gauge at once, while the incentive
        # tokens come out of each gauge separately. One button would have
        # to lie about one of them -- see `curve.earnings.ClaimPlan`.
        # `buttons.Themed`, not a plain button with a style taken once:
        # this page is built with the app, before the remembered theme has
        # been applied, so a style read at construction is Material's --
        # and a stadium among Chad's boxes is a control borrowed from
        # another program.
        self.claim_crv = buttons.Themed(
            "Claim CRV", page=page, on_click=lambda _e: self._claim(True),
            visible=False,
        )
        self.claim_rewards = buttons.Themed(
            "Claim rewards", page=page, on_click=lambda _e: self._claim(False),
            visible=False,
        )
        # Set like the total above it, which is the same kind of statement
        # about the same portfolio: the words in the body colour, the
        # figure bold. Two controls rather than one string, because one
        # string cannot be half bold -- and the weight is the whole point,
        # since it is what says which half you are meant to read.
        self.accrued_value = ft.Text("", size=BODY, weight=ft.FontWeight.BOLD)
        self.accrued_label = ft.Text(
            "", size=BODY, color=ft.Colors.ON_SURFACE_VARIANT
        )
        self.accrued = ft.Row(
            [self.accrued_label, self.accrued_value],
            spacing=6,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        #: The same panel the pool's Deposit and Withdraw tabs use -- see
        #: `ui.status`. A claim waits on the same wallet and the same
        #: block, and saying so in loose grey text made the portfolio look
        #: like a different program's idea of a pending transaction.
        self.status = StatusPanel(page)
        self.claim_status = self.status.text
        # The buttons at the left edge, under the page's title, where the
        # thing you came to press belongs. What is owed goes to the right,
        # under the total it is part of.
        #
        # **Wrapping, with no flex child.** Both halves matter and they
        # pull against each other:
        #
        #   * `Wrap` cannot lay out a flex child, so an `expand=True`
        #     spacer in here is a layout assertion rather than a stretchy
        #     gap -- and it fails silently, dropping the subtree and taking
        #     the table and its header with it while Python logs nothing.
        #     The page went blank for exactly as long as this bar was
        #     visible;
        #   * but dropping `wrap` to be rid of the spacer moves the problem
        #     to the other end: a label and two buttons in a fixed row
        #     overflow a phone.
        #
        # So the outer row does not wrap -- it is a plain `Row`, which
        # fills its width and can therefore hold a flex child. The buttons
        # take the flex and the label keeps its own size, which is what
        # puts one at each edge.
        #
        # Note what is *not* here: a spacer, and `SPACE_BETWEEN`. Neither
        # works in a `Wrap`, and for the same reason -- it sizes itself to
        # its content, so there is no free space to space anything between,
        # and a flex child is a layout assertion it fails silently.
        self._buttons = ft.Row(
            [
                buttons.shadowed(self.claim_crv, page),
                buttons.shadowed(self.claim_rewards, page),
            ],
            spacing=8,
            run_spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
        )
        self._claim_bar = ft.Container(visible=False)
        self._lay_out_claim_bar()

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
                        # Set on the same line as the figure it introduces,
                        # not shrunk beneath it: a caption a size smaller
                        # reads as a footnote about the number rather than
                        # as the first half of the sentence it is in.
                        # Weight is what tells them apart -- the label is
                        # the part you already know.
                        ft.Text("Total value:", size=METRIC,
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
        """Draw a set of positions, in the order the header asks for."""
        self._holdings = holdings
        self.rows.controls = [
            HoldingRow(
                holding, self._on_open, index, self._narrow,
                self._earnings.get(holding.address.lower()), self._crv,
                self._columns,
            )
            for index, holding in enumerate(self._in_order(holdings))
        ]
        self.total.value = compact_usd(sum(holding.value for holding in holdings))
        self._table.visible = bool(holdings)
        self.empty.visible = False
        safe_update(self)

    def _in_order(self, holdings: list[Holding]) -> list[Holding]:
        """Sorted by the active column, descending.

        Descending always, as on the pool list: nobody opens a portfolio
        looking for their smallest position, and a second click to reverse
        is a control that has to be explained.

        A position the earnings pass has not reached sorts *below* one
        earning nothing, on the two columns that pass fills in -- its key
        is -1 rather than 0. "Not read yet" is not a rate of zero, and
        leaving the unknowns mixed among the zeroes makes the order change
        under the reader as the read lands.
        """
        key = SORTS[self._sort][1]
        return sorted(
            holdings,
            key=lambda h: key(h, self._earnings.get(h.address.lower())),
            reverse=True,
        )

    def sort_by(self, key: str) -> None:
        """Order the table by a column. Same key again is not a reversal."""
        if key not in SORTS or key == self._sort:
            return
        self._sort = key
        self._sync_header()
        self.show(self._holdings)

    def _lay_out_claim_bar(self) -> None:
        """Buttons and label side by side, or stacked on a phone.

        Side by side they are at opposite edges, which is where each
        belongs: the buttons under the page's title, the figure under the
        total it is part of. There is no width at which both of those fit
        *and* a phone does, though -- two labelled buttons and a sum come
        to more than 400px however they are arranged -- so below the width
        at which the table becomes cards, the bar becomes two lines.
        """
        self._buttons.expand = not self._narrow
        self._claim_bar.content = (
            ft.Column([self._buttons, self.accrued], spacing=6)
            if self._narrow
            else ft.Row(
                [self._buttons, self.accrued],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

    def forget_earnings(self) -> None:
        """Drop what the last read found, without redrawing.

        Called when the page starts loading somebody else's positions.
        The two columns keep no numbers from the previous account -- they
        go back to an en dash, which says "not read" -- and the claim bar
        goes away rather than offering to claim what that account was
        owed. The earnings pass is the slowest of the three reads, so the
        window in which the wrong figures would be on screen is the
        longest one on the page.
        """
        self._earnings = {}
        self._plan = ClaimPlan()
        self._crv = ""
        self._claim_bar.visible = False
        self.status.say("")

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
        was = self._earnings
        self._earnings = {e.pool.lower(): e for e in earnings}
        entry = REWARDS.get(chain_id)
        self._crv = entry.crv if entry else ""
        self._plan = claim_plan(chain_id, earnings)
        owed = sum(e.claimable_value for e in earnings)
        crv = bool(self._plan.crv)
        extras = bool(self._plan.extras)

        self.claim_crv.visible = crv
        self.claim_rewards.visible = extras
        self.claim_crv.content = self._crv_label(earnings, len(self._plan.crv))
        self.claim_rewards.content = self._rewards_label(earnings)
        # No figure where nothing was priced: an unpriced reward is worth
        # something, and "$0" beside a claim button says it is not.
        self.accrued_label.value = "Unclaimed rewards:" if owed > 0 else (
            "Unclaimed rewards" if crv or extras else ""
        )
        self.accrued_value.value = compact_usd(owed) if owed > 0 else ""
        self._claim_bar.visible = crv or extras
        if self._sort in EARNED_SORTS and self._earnings != was:
            # The order itself depends on what just arrived, so the rows
            # are rebuilt rather than edited in place. Only for these two
            # columns: the other three are known from the scan, and a
            # rebuild of forty rows to change nothing about their order is
            # a flicker for no reason.
            self.show(self._holdings)
            return
        for row in self.rows.controls:
            if isinstance(row, HoldingRow):
                row.apply(self._earnings.get(row.holding.address.lower()), self._crv)
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

    def set_layout(self, layout: Layout) -> None:
        """Adopt a new layout: which columns fit, and cards or a table.

        The pool list's own `Layout`, rather than a second set of
        breakpoints -- the two tables are on the same page at the same
        width and there is no reading under which one of them should
        become cards while the other does not.
        """
        columns = (
            () if layout.cards
            else COLUMNS if layout.name == "wide"
            else COMPACT_COLUMNS
        )
        if layout.cards == self._narrow and columns == self._columns:
            return
        self._narrow = layout.cards
        self._columns = columns or COLUMNS
        self._header.visible = not layout.cards
        self._sync_header()
        self._lay_out_claim_bar()
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
        """Column headings, each one a click target that re-sorts the table.

        Plain `Container`s with `on_click` rather than `TextButton`s, for
        the reason `PoolListView._build_header` records: a TextButton here
        hovers correctly and never fires in the published web build, with
        no exception and no event. The container also makes the whole cell
        the hit target, which is the better affordance anyway.
        """
        self._sort_cells = {
            key: ft.Container(
                width=width,
                alignment=ft.Alignment.CENTER_RIGHT,
                padding=ft.Padding.symmetric(horizontal=6, vertical=8),
                on_click=lambda _e, k=key: self.sort_by(k),
                ink=True,
                border_radius=6,
            )
            for key, width in (
                ("wallet", W_WALLET), ("staked", W_STAKED), ("apr", W_APR),
                ("rewards", W_REWARDS), ("value", W_VALUE),
            )
        }
        self._sync_header()
        return ft.Container(
            ft.Row(
                [
                    ft.Container(
                        ft.Text("Pool", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
                        expand=True,
                    ),
                    *(self._sort_cells[key] for key in COLUMNS),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=2),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE)),
            bgcolor=theme.header_bg(self._page),
            visible=not self._narrow,
        )

    def _sync_header(self) -> None:
        """Mark the column being sorted on, and hide the ones not drawn.

        The arrow is a Material icon rather than a "↓" in the label: the
        published web build's font has no glyph for that character and
        renders a tofu box, while the icon font is bundled and works on
        both platforms. Same reasoning, and the same icon, as the pool
        list -- these are two views of one table style.
        """
        for key, cell in self._sort_cells.items():
            cell.visible = key in self._columns
            active = key == self._sort
            label = ft.Text(
                SORTS[key][0],
                size=SMALL,
                weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL,
                color=ft.Colors.PRIMARY if active else ft.Colors.ON_SURFACE_VARIANT,
            )
            cell.content = ft.Row(
                [label] + (
                    [ft.Icon(ft.Icons.ARROW_DOWNWARD, size=BODY,
                             color=ft.Colors.PRIMARY)]
                    if active else []
                ),
                spacing=2,
                tight=True,
                alignment=ft.MainAxisAlignment.END,
            )
