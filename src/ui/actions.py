"""The four things you can do to a pool: deposit, withdraw, swap, stake.

Note the coin list a panel uses is `pool.pool_coins`, not `pool.coins`. On
a metapool those differ: v2 decomposes the base pool into `coins`, so a
two-coin metapool reports four. `add_liquidity` takes a `uint256[N]` whose
N is part of the function signature, so building it from the decomposed
list is calldata for a function the pool does not have.

The exceptions are the two underlying routes, which exist precisely to
take the decomposed list. Depositing and withdrawing them needs a zap --
see `curve.zaps` -- and each panel keeps a set of fields per route: which
one is live decides the coins, the spender that gets approved, the
contract the transaction goes to, and the fee the slippage comes from.

**Swapping them needs nothing.** A metapool does the base-pool leg itself
through `exchange_underlying`, so that route approves the pool like any
other swap and works on every chain, including the ones where no zap was
ever deployed.

Each tab is the same shape -- read some balances, quote the result on
chain, then submit -- so the shared parts (the approve step, the status
line, the "connect a wallet first" state) live in `ActionTab` and each
subclass only supplies its own fields and its own two transactions.

Two rules hold everywhere in this file:

  * amounts are integers in the token's smallest unit from the moment they
    are parsed until they reach calldata. Floats appear only in labels.
  * every quote is fetched from the pool itself rather than computed here.
    Re-implementing Curve's invariants in Python would be a second source
    of truth that silently drifts from the deployed contract.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Awaitable, Callable

import flet as ft

from curve.abi import FEE_DENOMINATOR, apply_slippage
from curve.confirm import POLL_INTERVAL, wait_for_confirmation
from curve.format import token_amount, units_to_float
from curve.models import Pool
from curve.pool import PoolContract
from curve.zaps import zap_for
from wallet.base import WalletError

from .assets import chain_name
from .logos import pool_stack, token_mark
from .typography import BODY, LABEL, SMALL
from wallet.erc20 import format_units, parse_units

#: How often to ask whether a transaction has been mined. A module-level
#: knob rather than a hidden default so a test can run the loop flat out.
CONFIRM_INTERVAL = POLL_INTERVAL

#: Tolerance used until the pool says otherwise, and whenever it will not.
#: Curve shows 0.03% on pegged stable pools, but one number has to cover
#: every pool type here, so it errs toward the volatile end.
DEFAULT_SLIPPAGE = 0.5

#: "no fee read yet" -- distinct from None, which is what the deposit and
#: withdraw panels use as their key.
_NOT_READ = object()

#: How much of the pool's own fee to allow as slippage, for an action whose
#: quote is exact.
#:
#: The fee is what a trade is expected to cost, so it is the natural scale
#: for what an unexpected move is worth tolerating -- a tricrypto pool
#: charging 0.046% and a stable pool charging 0.01% do not deserve the same
#: fixed 0.5%. A fifth is tight, which is the point: `get_dy` is fetched
#: immediately before the transaction, so this only covers movement between
#: the quote and the block.
SLIPPAGE_OF_FEE = 0.2

#: The deposit and withdrawal side, as `a * fee + b`, fitted to what real
#: deposits need: 88 measurements over 44 mainnet pools, run on a fork
#: through titanoboa, bisecting `min_mint` until `add_liquidity` stops
#: reverting.
#:
#: `a = 1`: the whole fee. With `b` held at 0.005% the smallest slope that
#: covers all 44 pools is 0.880 -- binding, as always, on cvxCrv/Crv -- and
#: rounding it to one is not just headroom, it is the ceiling the number is
#: converging to. The imbalance fee on a fully single-sided deposit
#: approaches the base fee as a two-coin pool skews (Curve's adjusted fee
#: is `base * N / (4(N-1))`, applied to each coin's distance from balance),
#: so no pool of this shape can need more. The measured worst was 0.91.
#:
#: Which makes the whole rule sayable in one line: *a deposit may lose its
#: pool's fee, plus a little for the quote going stale.* What binds it is
#: the old StableSwap implementations, whose `calc_token_amount` quotes
#: fee-free so the mint lands short: cvxCrv/Crv by 0.91x its fee, 3pool by
#: 0.63x, alETH/frxETH by 0.13x, msETH/WETH by 0.05x -- the spread is how
#: imbalanced each pool already is, and one whole fee is the ceiling for a
#: two-coin pool. Everything modern (crvusd, stableswap-ng, tricrypto-ng,
#: twocrypto-ng, the old crypto pools) mints *exactly* the quote at every
#: size from 0.01% to 1% of the pool, so for those this is pure margin --
#: which is also what covers an implementation this app has never seen.
ESTIMATE_FEE_SHARE = 1.0

#: `b` is what the same fit says is zero -- and it is zero *across pools*,
#: because a cross-section cannot see time. Every pool above was measured
#: at one moment; what a quote loses by being a few blocks old is a
#: separate axis, measured by calling `calc_token_amount` against the
#: archive at the block you would have quoted at and the block you land in.
#:
#: That is bursty rather than pool-shaped: in a quiet snapshot the largest
#: drop over five blocks across all 44 pools was 0.00022%, while a volatile
#: one cost 0.0173% over three blocks on TricryptoUSDC. Keeping `b` at
#: 0.005% covers the quiet case outright, and the bursty one is covered by
#: the fee term instead: the pools that lurch are the ones charging enough
#: for `a * fee` to absorb it (TricryptoUSDC allows 0.0335% against that
#: 0.0173% drop), while the pools with fees too small to help are the
#: pegged ones that do not move -- Strategic USD Reserves charges 0.001%
#: and its worst drift over a hundred blocks was 0.0019%, inside 0.00595%.
#:
#: Which is the whole reason to keep the slope: a low constant is only
#: affordable because `a` is doing the work where the risk actually is.
QUOTE_DRIFT = 0.005


def slippage_for(
    fee_units: int, multiple: float = SLIPPAGE_OF_FEE, constant: float = 0.0
) -> float:
    """`a * fee + b`, in percent, from a fee in Curve's 1e10 units."""
    return fee_units / FEE_DENOMINATOR * 100 * multiple + constant


def format_slippage(percent: float) -> str:
    """Three significant figures, rounded *up*.

    Up rather than nearest because this number is a floor being handed to
    the chain: `%.3g` turns 0.01925 into 0.0192, which is a tolerance
    slightly tighter than the one that was calculated, and the whole point
    of calculating it was that tighter reverts.
    """
    if percent <= 0:
        return "0"
    value = Decimal(repr(percent))
    step = Decimal(1).scaleb(value.adjusted() - 2)      # third significant digit
    rounded = value.quantize(step, rounding=ROUND_CEILING)
    text = format(rounded, "f").rstrip("0").rstrip(".")
    return text or "0"


def _status_tint(colour: str | None, pending: bool) -> str:
    """The panel behind a status line, keyed to what it is saying."""
    if pending:
        return ft.Colors.with_opacity(0.10, ft.Colors.PRIMARY)
    if colour == ft.Colors.ERROR:
        return ft.Colors.with_opacity(0.10, ft.Colors.ERROR)
    if colour == ft.Colors.GREEN_600:
        return ft.Colors.with_opacity(0.12, ft.Colors.GREEN_600)
    return ft.Colors.with_opacity(0.06, ft.Colors.ON_SURFACE)


def _stacked(*controls: ft.Control) -> ft.Column:
    """A field with its caption underneath, both as wide as the panel."""
    return ft.Column(
        list(controls),
        spacing=2,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def _aside(control: ft.Control) -> ft.Row:
    """A control that should keep its own size, pushed to the right."""
    return ft.Row([control], alignment=ft.MainAxisAlignment.END, tight=True)


class ActionTab:
    """Base for the four panels. Subclasses build fields and submit."""

    title = ""
    #: Label for the button that sends the main transaction.
    submit_label = "Confirm"
    #: Past tense, for the line shown once the transaction is mined.
    done_verb = "Confirmed"
    #: Does this action have a price to protect? Staking does not -- it
    #: moves LP tokens into a gauge at no rate at all -- so showing a
    #: tolerance there invites someone to tune a number that does nothing.
    uses_slippage = True
    #: `a` and `b` for this action. Deposits and withdrawals are quoted by
    #: `calc_token_amount`, which some implementations compute fee-free.
    fee_multiple = ESTIMATE_FEE_SHARE
    slippage_constant = QUOTE_DRIFT

    def __init__(
        self,
        page: ft.Page,
        pool: Pool,
        get_contract: Callable[[], PoolContract | None],
        on_done: Callable[[], Awaitable[None]],
    ) -> None:
        self.page = page
        self.pool = pool
        self.get_contract = get_contract
        self.on_done = on_done

        # Secondary, and sized to say so: a number you set once and then
        # ignore should not have the same weight as the amount you are
        # about to send.
        self.slippage = ft.TextField(
            label="Slippage %",
            value=str(DEFAULT_SLIPPAGE),
            width=92,
            dense=True,
            text_size=LABEL,
            label_style=ft.TextStyle(size=LABEL),
            on_change=self._slippage_edited,
        )
        #: Once the user has typed in the box, the pool stops overwriting it.
        self._slippage_is_theirs = False
        #: What the last suggestion was for, so a fee is read once per pair
        #: rather than on every keystroke.
        self._fee_read_for: object = _NOT_READ
        self.status = ft.Text("", size=SMALL, selectable=True, expand=True)
        #: Shown while a transaction is in flight. A wallet takes seconds
        #: and a block takes twelve, and a line of text that simply sits
        #: there is indistinguishable from one that has stopped.
        self.status_spinner = ft.ProgressRing(width=14, height=14, stroke_width=2)
        #: The status line as a whole: a tinted panel rather than loose
        #: text, so "waiting", "done" and "failed" are told apart before
        #: the words are read.
        self.status_panel = ft.Container(
            ft.Row([self.status_spinner, self.status], spacing=10, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=8,
            visible=False,
        )
        self.estimate = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)

        # Every read here goes through the *wallet's* provider, so it lands
        # on whatever network the wallet is on -- not the one being
        # browsed. Picking Gnosis in the header while the wallet sits on
        # Ethereum therefore quotes Gnosis addresses against Ethereum,
        # where they hold no code, and every estimate comes back "the pool
        # did not answer". That looked like a pool this app could not
        # handle. It is worth saying plainly instead, with the one button
        # that fixes it.
        self.network_note = ft.Text("", size=SMALL, expand=True)
        self.switch_button = ft.TextButton("Switch", on_click=self._switch_network)
        self.network_panel = ft.Container(
            ft.Row([self.network_note, self.switch_button], spacing=8, tight=True),
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.TERTIARY),
            visible=False,
        )

        # Labels go through `content`, not `text`: `ft.Button` has no `text`
        # property, so assigning one sets an attribute nobody reads and the
        # button keeps whatever it was built with. That is how Unstake stayed
        # labelled "Stake", and why the numbered "2. Deposit" step never
        # appeared next to "1. Approve".
        self.approve_button = ft.Button(
            "1. Approve", on_click=self._approve_clicked, visible=False, disabled=True
        )
        self.submit_button = ft.Button(
            self.submit_label, on_click=self._submit_clicked, disabled=True
        )
        # STRETCH, so every field fills the panel. Without it a Column
        # gives each child its intrinsic width, and Material's idea of how
        # wide a text field wants to be left the amounts ending short of
        # the right edge -- by about the width of the slippage box, which
        # made it look like they were dodging it.
        self.control = ft.Column(
            spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
        )

    # -- to implement -----------------------------------------------------

    def build(self) -> list[ft.Control]:
        raise NotImplementedError

    async def refresh(self) -> None:
        """Re-read balances and re-quote. Must tolerate no wallet."""

    async def fee_units(self, contract: PoolContract) -> int:
        """The fee this action pays, in Curve's 1e10 units."""
        return await contract.fee()

    def fee_key(self) -> object:
        """What the fee depends on, so it is re-read when that changes."""
        return None

    async def suggest_slippage(self, contract: PoolContract | None) -> None:
        """Set the tolerance from the pool's own fee, once, quietly.

        Never against what the user typed: the moment they touch the box it
        is theirs. And never on a failed read -- a pool that will not answer
        `fee()` leaves the default standing rather than an empty field.
        """
        if contract is None or not self.uses_slippage or self._slippage_is_theirs:
            return
        key = self.fee_key()
        if key == self._fee_read_for:
            return
        try:
            fee = await self.fee_units(contract)
        except WalletError:
            return
        self._fee_read_for = key
        if fee <= 0:
            return
        percent = slippage_for(fee, self.fee_multiple, self.slippage_constant)
        self.slippage.value = format_slippage(percent)
        self.slippage.tooltip = (
            f"from this pool's {fee / FEE_DENOMINATOR * 100:.4g}% fee"
        )

    async def submit(self, contract: PoolContract) -> str:
        raise NotImplementedError

    def clear_inputs(self) -> None:
        """Empty the amount fields after a confirmed transaction.

        The number that was there has been spent; leaving it in place
        invites sending it twice.
        """

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        """Return `(token, spender, amount)` still needing an allowance."""
        return None

    # -- shared behaviour -------------------------------------------------

    def mount(self) -> ft.Column:
        self.control.controls = [
            self.network_panel,
            *self.build(),
            # Next to the amounts it protects. Down by the button it read
            # as part of the action rather than as a setting for it.
            *([_aside(self.slippage)] if self.uses_slippage else []),
            self.estimate,
            self.approve_button,
            self.submit_button,
            self.status_panel,
        ]
        return self.control

    async def network_ok(self, contract: PoolContract | None) -> bool:
        """Is the wallet on the network these pools are on?

        False also hides the panel's estimate and greys its buttons, since
        nothing can be read or sent across a network boundary. A chain the
        app does not know the id of (0, which is what a pool built from a
        partial payload has) is not something to complain about.
        """
        if contract is None or not self.pool.chain_id:
            self.network_panel.visible = False
            return True
        try:
            current = await contract.provider.chain_id()
        except WalletError:
            self.network_panel.visible = False
            return True
        matched = current == self.pool.chain_id
        self.network_panel.visible = not matched
        if not matched:
            wanted = chain_name(self.pool.chain) or f"chain {self.pool.chain_id}"
            self.network_note.value = (
                f"Your wallet is on another network. Switch it to {wanted} to "
                "read balances or act on this pool."
            )
            self.switch_button.content = f"Switch to {wanted}"
            self.estimate.value = ""
            self.approve_button.visible = False
            self.submit_button.disabled = True
        return matched

    async def _switch_network(self, _e: ft.ControlEvent) -> None:
        """Ask the wallet to move. It may refuse, or not know the chain."""
        contract = self.get_contract()
        if contract is None:
            return
        try:
            await contract.provider.switch_chain(self.pool.chain_id)
        except WalletError as exc:
            self._say(str(exc), ft.Colors.ERROR)
            return
        # The wallet emits `chainChanged`, which the app follows; refresh
        # anyway in case it was already there and simply said nothing.
        await self.refresh()

    def _slippage_edited(self, _e: ft.ControlEvent) -> None:
        self._slippage_is_theirs = True

    def slippage_pct(self) -> float:
        try:
            value = float((self.slippage.value or "").strip())
        except ValueError:
            return DEFAULT_SLIPPAGE
        return value if 0 <= value < 100 else DEFAULT_SLIPPAGE

    def with_slippage(self, amount: int) -> int:
        return apply_slippage(amount, self.slippage_pct())

    def _say(
        self, message: str, colour: str | None = None, *, pending: bool = False
    ) -> None:
        """Show a status. `pending` means a spinner and a neutral tint.

        The tint is derived from the text colour rather than passed in, so
        a caller cannot say "green" and get an amber panel.
        """
        self.status.value = message
        self.status.color = colour or ft.Colors.ON_SURFACE_VARIANT
        self.status_spinner.visible = pending
        self.status_panel.visible = bool(message)
        self.status_panel.bgcolor = _status_tint(colour, pending)
        self.page.update()

    def _busy(self, busy: bool) -> None:
        self.submit_button.disabled = busy
        self.approve_button.disabled = busy
        self.page.update()

    async def _confirm(self, contract: PoolContract, tx: str, done: str) -> None:
        """Wait for the transaction, then let the panel read the result.

        Everything the panel shows next -- the allowance that ungreys the
        submit button, the balances, the position -- is read back from the
        chain, and reading it while the transaction is still in the mempool
        shows the state before it. That is why an approval used to land and
        leave the button disabled.

        The block the receipt named still governs those reads; it is simply
        not worth saying. What the line reports is what was sent, in the
        numbers that were typed.
        """
        self._say(f"Waiting for {tx[:14]}… to confirm.", pending=True)
        await wait_for_confirmation(contract.provider, tx, interval=CONFIRM_INTERVAL)
        self._say(done, ft.Colors.GREEN_600)

    def amount_label(self, address: str, amount: int) -> str:
        """`1,000 USDC` -- an amount in the units of whatever token it is.

        Every coin the pool knows about, decomposed list included, so this
        also names the underlying coins the zap route deals in. Anything
        else is the LP token, which has no entry of its own.
        """
        for coin in self.pool.coins:
            if coin.address.lower() == address.lower():
                return (
                    f"{token_amount(units_to_float(amount, coin.decimals))} "
                    f"{coin.symbol}"
                )
        return f"{token_amount(units_to_float(amount, 18))} LP"

    def summary(self) -> str:
        """What the pending transaction does, for the confirmation line."""
        return ""

    def done_message(self) -> str:
        summary = self.summary()
        return f"{self.done_verb} {summary}." if summary else f"{self.done_verb}."

    async def _approve_clicked(self, _e: ft.ControlEvent) -> None:
        contract = self.get_contract()
        if contract is None:
            self._say("Connect a wallet first.", ft.Colors.ERROR)
            return
        self._busy(True)
        try:
            pending = await self.approval_needed(contract)
            if pending is None:
                self._say("Already approved.")
            else:
                token, spender, amount = pending
                self._say("Confirm the approval in your wallet…", pending=True)
                tx = await contract.approve(token, spender, amount)
                await self._confirm(
                    contract, tx, f"Approved {self.amount_label(token, amount)}."
                )
        except WalletError as exc:
            self._say(str(exc), ft.Colors.ERROR)
        finally:
            self._busy(False)
            await self.refresh()

    async def _submit_clicked(self, _e: ft.ControlEvent) -> None:
        contract = self.get_contract()
        if contract is None:
            self._say("Connect a wallet first.", ft.Colors.ERROR)
            return
        self._busy(True)
        try:
            self._say("Confirm in your wallet…", pending=True)
            # Read before sending: `clear_inputs` empties the fields the
            # summary is built from.
            done = self.done_message()
            tx = await self.submit(contract)
            await self._confirm(contract, tx, done)
            self.clear_inputs()
            await self.on_done()
        except WalletError as exc:
            self._say(str(exc), ft.Colors.ERROR)
        finally:
            self._busy(False)
            await self.refresh()

    async def _sync_approval(self, contract: PoolContract | None) -> None:
        """Show or hide the approve step based on the current allowance."""
        if contract is None:
            self.approve_button.visible = False
            self.submit_button.disabled = True
            return
        try:
            pending = await self.approval_needed(contract)
        except WalletError:
            pending = None
        self.approve_button.visible = pending is not None
        self.approve_button.disabled = pending is None
        self.submit_button.content = (
            f"2. {self.submit_label}" if pending is not None else self.submit_label
        )
        # Curve gates the action behind the approval; so does this, because
        # a deposit sent without one just reverts and costs gas.
        self.submit_button.disabled = pending is not None


def _max_button(on_click) -> ft.TextButton:
    """The "MAX" affordance that lives inside an amount field.

    Small and quiet: it is a shortcut for typing, not an action, and it
    sits inside the box it fills rather than under it.
    """
    return ft.TextButton(
        "MAX",
        on_click=on_click,
        style=ft.ButtonStyle(
            padding=ft.Padding.symmetric(horizontal=8),
            text_style=ft.TextStyle(size=LABEL, weight=ft.FontWeight.BOLD),
        ),
    )


def _amount_field(
    label: str, on_change, mark: ft.Control | None = None, on_max=None
) -> ft.TextField:
    """An amount input, with the token's mark in front of it.

    `prefix_icon`, not `prefix`: Material only reveals a `prefix` once the
    field is focused or has text, so a logo put there is invisible exactly
    when it is most useful -- before you have typed anything.

    The slot is sized from the mark itself, because an LP field's mark is a
    stack of every coin in the pool and would otherwise be clipped to the
    width of one.
    """
    constraints = None
    if mark is not None:
        width = (getattr(mark, "width", None) or 20) + 16
        # Material gives the icon slot no inset of its own, so a wide mark
        # ends up flush against the field's border. The padding is part of
        # the mark rather than the constraint because the constraint only
        # reserves space -- it does not move anything.
        height = (getattr(mark, "height", None) or 20) + 12
        mark = ft.Container(
            mark,
            width=width,
            height=height,
            padding=ft.Padding.only(left=10),
            alignment=ft.Alignment.CENTER_LEFT,
        )
        constraints = ft.BoxConstraints(min_width=width, min_height=height)
    return ft.TextField(
        label=label,
        hint_text="0.0",
        dense=True,
        on_change=on_change,
        prefix_icon=mark,
        prefix_icon_size_constraints=constraints,
        suffix_icon=_max_button(on_max) if on_max is not None else None,
        suffix_icon_size_constraints=(
            ft.BoxConstraints(min_width=58, min_height=28) if on_max else None
        ),
    )


class _AmountRows:
    """The amount fields for one deposit route, as a block that can hide.

    A metapool has two of these -- one per coin the contract holds, one per
    underlying coin the zap accepts -- and they are built together and
    swapped by visibility rather than rebuilt on every toggle, so a number
    typed into one is still there after a look at the other.
    """

    def __init__(self, coins, chain: str, on_change, on_max) -> None:
        self.coins = list(coins)
        self.fields: list[ft.TextField] = []
        self.labels: list[ft.Text] = []
        self.balances: list[int] = [0] * len(self.coins)
        rows: list[ft.Control] = []
        for index, coin in enumerate(self.coins):

            def fill(_e: ft.ControlEvent, index: int = index) -> None:
                on_max(self, index)

            field = _amount_field(
                coin.symbol, on_change, token_mark(coin, chain, 20), on_max=fill
            )
            label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
            self.fields.append(field)
            self.labels.append(label)
            rows.append(_stacked(field, label))
        self.control = ft.Column(
            rows, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH
        )

    def amounts(self) -> list[int]:
        out = []
        for field, coin in zip(self.fields, self.coins):
            text = (field.value or "").strip()
            try:
                out.append(parse_units(text, coin.decimals) if text else 0)
            except ValueError:
                out.append(0)
        return out

    def clear(self) -> None:
        for field in self.fields:
            field.value = ""


def underlying_swap_spender(pool: Pool) -> str | None:
    """Who moves the coins for an underlying swap, or None if nobody can.

    A StableSwap metapool swaps its underlying itself, so the pool is the
    spender as it is for any other swap. A crypto metapool has no
    `exchange_underlying` at all and its per-pool zap carries one instead
    -- so there the zap is what gets approved. The factory zaps of either
    kind have no swap functions, so a crypto metapool served only by one
    of those has no underlying swap route at all.
    """
    if not pool.has_underlying:
        return None
    if pool.is_stableswap:
        return pool.address
    zap = zap_for(pool)
    return zap.address if zap is not None and zap.swaps else None


def _route_picker(on_change, *, underlying: bool) -> ft.RadioGroup:
    """Underlying or pool tokens: which coins the amounts are denominated in.

    Underlying first, and selected, wherever it is available: it is the one
    denominated in coins people actually hold. The base pool's LP token is
    the specialist case -- you have some only if you went and made some.
    """
    return ft.RadioGroup(
        value="underlying" if underlying else "pool",
        content=ft.Row(
            [
                ft.Radio(value="underlying", label="Underlying"),
                ft.Radio(value="pool", label="Pool tokens"),
            ]
        ),
        on_change=on_change,
    )


def _coin_options(coins, chain: str) -> list[ft.DropdownOption]:
    """Dropdown entries carrying each coin's mark beside its symbol."""
    return [
        ft.DropdownOption(
            key=str(index),
            text=coin.symbol,
            content=ft.Row(
                [token_mark(coin, chain, 18), ft.Text(coin.symbol, size=BODY)],
                spacing=8,
                tight=True,
            ),
        )
        for index, coin in enumerate(coins)
    ]


class DepositTab(ActionTab):
    """Add liquidity: an amount per coin, quoted as LP tokens out.

    On a metapool there are two sets of coins to offer. The contract holds
    two -- its own, and the base pool's LP token -- but almost nobody holds
    that LP token; what they have is USDC. So when a zap exists for this
    pool, a second route appears whose fields are the *underlying* coins,
    and everything downstream (the quote, the approvals, the deposit, and
    the fee the slippage comes from) follows the chosen route.
    """

    title = "Deposit"
    submit_label = "Deposit"
    done_verb = "Deposited"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.zap = zap_for(self.pool)
        self.route = _route_picker(
            self._route_changed, underlying=self.zap is not None
        )
        self.route.visible = self.zap is not None
        self.routes = {
            "pool": _AmountRows(
                self.pool.pool_coins, self.pool.chain, self._changed, self._max_for
            )
        }
        if self.zap is not None:
            self.routes["underlying"] = _AmountRows(
                self.pool.display_coins, self.pool.chain, self._changed, self._max_for
            )
        self._apply_route()
        self._expected_lp = 0
        #: Whether the last quote came back. A zap this app has the address
        #: of but the chain disagrees with must not be handed an approval,
        #: so the approve step waits for a quote that worked.
        self._quote_ok = True

    # -- which route is live ----------------------------------------------

    @property
    def underlying(self) -> bool:
        return self.zap is not None and self.route.value == "underlying"

    @property
    def rows(self) -> _AmountRows:
        return self.routes["underlying" if self.underlying else "pool"]

    @property
    def spender(self) -> str:
        """Who moves the coins: the pool itself, or the zap."""
        return self.zap.address if self.underlying and self.zap else self.pool.address

    # The panel's own fields, as the rest of the class and the tests know
    # them -- always those of the live route.
    @property
    def fields(self) -> list[ft.TextField]:
        return self.rows.fields

    @property
    def balance_labels(self) -> list[ft.Text]:
        return self.rows.labels

    @property
    def balances(self) -> list[int]:
        return self.rows.balances

    @balances.setter
    def balances(self, values: list[int]) -> None:
        self.rows.balances = list(values)

    def build(self) -> list[ft.Control]:
        return [self.route, *(rows.control for rows in self.routes.values())]

    def _apply_route(self) -> None:
        """Show the live route's fields and hide the other's."""
        live = "underlying" if self.underlying else "pool"
        for name, rows in self.routes.items():
            rows.control.visible = name == live

    def _route_changed(self, _e: ft.ControlEvent) -> None:
        self._apply_route()
        self.page.run_task(self.refresh)

    def _max_for(self, rows: _AmountRows, index: int) -> None:
        """Fill one coin's field with the whole wallet balance.

        Full precision, not the shortened form the balance line shows:
        this is the number that becomes calldata, and a rounded one would
        either leave dust or exceed what is there.
        """
        coin = rows.coins[index]
        rows.fields[index].value = format_units(
            rows.balances[index], coin.decimals, precision=coin.decimals
        )
        self.page.run_task(self.refresh)

    def clear_inputs(self) -> None:
        for rows in self.routes.values():
            rows.clear()

    def _amounts(self) -> list[int]:
        return self.rows.amounts()

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh)

    def summary(self) -> str:
        rows = self.rows
        parts = [
            self.amount_label(coin.address, amount)
            for coin, amount in zip(rows.coins, rows.amounts())
            if amount > 0
        ]
        return " + ".join(parts)

    async def _quote(self, contract: PoolContract, amounts: list[int]) -> int:
        if self.underlying:
            return await contract.zap_calc_token_amount(amounts, deposit=True)
        return await contract.calc_token_amount(amounts, deposit=True)

    async def fee_units(self, contract: PoolContract) -> int:
        """A zap deposit passes through both pools, so it pays both fees."""
        fee = await contract.fee()
        if self.underlying:
            try:
                fee += await contract.base_fee()
            except WalletError:
                pass
        return fee

    def fee_key(self) -> object:
        return self.route.value

    async def refresh(self) -> None:
        contract = self.get_contract()
        if not await self.network_ok(contract):
            self.page.update()
            return
        await self.suggest_slippage(contract)
        rows = self.rows
        if contract is not None:
            for index, coin in enumerate(rows.coins):
                try:
                    rows.balances[index] = await contract.balance_of(coin.address)
                except WalletError:
                    rows.balances[index] = 0
                rows.labels[index].value = (
                    f"Balance: {format_units(rows.balances[index], coin.decimals)}"
                )

        amounts = rows.amounts()
        self._expected_lp = 0
        self._quote_ok = True
        if contract is not None and any(amounts):
            try:
                self._expected_lp = await self._quote(contract, amounts)
                self.estimate.value = (
                    f"~ {token_amount(units_to_float(self._expected_lp, 18))} LP"
                    f"  (min {token_amount(units_to_float(self.with_slippage(self._expected_lp), 18))})"
                )
            except WalletError as exc:
                self.estimate.value = str(exc)
                self._quote_ok = False
        else:
            self.estimate.value = ""

        await self._sync_approval(contract)
        if contract is not None and (not any(amounts) or not self._quote_ok):
            self.submit_button.disabled = True
        self.page.update()

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        # One coin at a time: each ERC-20 needs its own approval, and the UI
        # walks them in order rather than batching, so the button always
        # names a single concrete step.
        #
        # Nothing is approved on the strength of a quote that failed: the
        # spender may be a zap, and an allowance is the one thing here that
        # outlives the mistake that caused it.
        if not self._quote_ok:
            return None
        rows = self.rows
        for coin, amount in zip(rows.coins, rows.amounts()):
            if amount <= 0:
                continue
            allowance = await contract.allowance(coin.address, self.spender)
            if allowance < amount:
                self.approve_button.content = f"1. Approve {coin.symbol}"
                return (coin.address, self.spender, amount)
        return None

    async def submit(self, contract: PoolContract) -> str:
        amounts = self._amounts()
        if not any(amounts):
            raise WalletError("Enter an amount to deposit.")
        expected = await self._quote(contract, amounts)
        floor = self.with_slippage(expected)
        if self.underlying:
            return await contract.zap_add_liquidity(amounts, floor)
        return await contract.add_liquidity(amounts, floor)


class WithdrawTab(ActionTab):
    """Remove liquidity, either balanced or all into one coin."""

    title = "Withdraw"
    submit_label = "Withdraw"
    done_verb = "Withdrew"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lp_balance = 0
        self.zap = zap_for(self.pool)
        # An LP token has no logo of its own; Curve draws the pool's coins.
        self.amount = _amount_field(
            "LP tokens",
            self._changed,
            pool_stack(self.pool, 18, limit=4),
            on_max=self._max,
        )
        self.lp_label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.route = _route_picker(
            self._route_changed, underlying=self.zap is not None
        )
        self.route.visible = self.zap is not None
        # Kept as its own attribute so the zap route can grey it out: a zap
        # withdrawal goes into one coin, and there is nothing to quote a
        # balanced one against -- the base pool's reserves are not on the
        # metapool, and a floor of zero is no floor at all.
        self.balanced_radio = ft.Radio(value="balanced", label="All coins")
        self.mode = ft.RadioGroup(
            value="balanced",
            content=ft.Row([self.balanced_radio, ft.Radio(value="one", label="One coin")]),
            on_change=self._changed,
        )
        self.coin_picker = ft.Dropdown(
            label="Receive",
            options=_coin_options(self.pool.pool_coins, self.pool.chain),
            value="0",
            dense=True,
            visible=False,
            leading_icon=token_mark(self.pool.pool_coins[0], self.pool.chain, 20)
            if self.pool.pool_coins
            else None,
            on_select=self._changed,
        )
        self._quote_ok = True
        # The zap route is the default where it exists, so the receive list
        # and the single-coin rule have to hold from the start rather than
        # from the first time the switch is touched.
        self._apply_route()

    # -- which route is live ----------------------------------------------

    @property
    def underlying(self) -> bool:
        return self.zap is not None and self.route.value == "underlying"

    @property
    def coins(self) -> list:
        return self.pool.display_coins if self.underlying else self.pool.pool_coins

    def build(self) -> list[ft.Control]:
        return [
            _stacked(self.amount, self.lp_label),
            self.route,
            self.mode,
            self.coin_picker,
        ]

    def _apply_route(self) -> None:
        """Swap the receive list, and force the single-coin mode on a zap."""
        self.coin_picker.options = _coin_options(self.coins, self.pool.chain)
        self.coin_picker.value = "0"
        self.balanced_radio.disabled = self.underlying
        self.balanced_radio.tooltip = (
            "A zap withdrawal goes into one coin at a time."
            if self.underlying
            else None
        )
        if self.underlying:
            self.mode.value = "one"
        self.coin_picker.visible = self.mode.value == "one"
        coins = self.coins
        if coins:
            self.coin_picker.leading_icon = token_mark(coins[0], self.pool.chain, 20)

    def _route_changed(self, _e: ft.ControlEvent) -> None:
        self._apply_route()
        self._changed(None)

    def _max(self, _e: ft.ControlEvent) -> None:
        self.amount.value = format_units(self.lp_balance, 18, precision=18)
        self.page.run_task(self.refresh)

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.coin_picker.visible = self.mode.value == "one"
        coins = self.coins
        index = self._coin_index()
        if 0 <= index < len(coins):
            self.coin_picker.leading_icon = token_mark(coins[index], self.pool.chain, 20)
        self.page.run_task(self.refresh)

    def clear_inputs(self) -> None:
        self.amount.value = ""

    def _lp_amount(self) -> int:
        text = (self.amount.value or "").strip()
        try:
            return parse_units(text, 18) if text else 0
        except ValueError:
            return 0

    def _coin_index(self) -> int:
        try:
            return int(self.coin_picker.value or "0")
        except ValueError:
            return 0

    def summary(self) -> str:
        amount = self._lp_amount()
        if amount <= 0:
            return ""
        label = self.amount_label(self.pool.lp_token, amount)
        if self.mode.value == "one":
            coins = self.coins
            index = self._coin_index()
            if index < len(coins):
                return f"{label} for {coins[index].symbol}"
        return label

    async def fee_units(self, contract: PoolContract) -> int:
        """A zap withdrawal comes back out through both pools."""
        fee = await contract.fee()
        if self.underlying:
            try:
                fee += await contract.base_fee()
            except WalletError:
                pass
        return fee

    def fee_key(self) -> object:
        return self.route.value

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        """LP tokens the zap has to be allowed to take.

        Only on the zap route. Burning LP at the pool needs no approval --
        the pool burns the caller's own balance rather than transferring it
        -- but a zap is a third party, so it has to be allowed to move the
        LP before it can burn it on your behalf.
        """
        if not self.underlying or self.zap is None or not self._quote_ok:
            return None
        amount = self._lp_amount()
        if amount <= 0:
            return None
        allowance = await contract.allowance(self.pool.lp_token, self.zap.address)
        if allowance >= amount:
            return None
        self.approve_button.content = "1. Approve LP"
        return (self.pool.lp_token, self.zap.address, amount)

    async def refresh(self) -> None:
        contract = self.get_contract()
        if not await self.network_ok(contract):
            self.page.update()
            return
        await self.suggest_slippage(contract)
        amount = self._lp_amount()
        if contract is not None:
            try:
                self.lp_balance = await contract.lp_balance()
                self.lp_label.value = f"Balance: {format_units(self.lp_balance, 18)} LP"
            except WalletError:
                self.lp_label.value = ""

        self.estimate.value = ""
        self._quote_ok = True
        if contract is not None and amount > 0 and self.mode.value == "one":
            index = self._coin_index()
            coins = self.coins
            coin = coins[index] if index < len(coins) else coins[0]
            try:
                out = await (
                    contract.zap_calc_withdraw_one_coin(amount, index)
                    if self.underlying
                    else contract.calc_withdraw_one_coin(amount, index)
                )
                self.estimate.value = (
                    f"~ {token_amount(units_to_float(out, coin.decimals))} {coin.symbol}"
                    f"  (min {token_amount(units_to_float(self.with_slippage(out), coin.decimals))})"
                )
            except WalletError as exc:
                self.estimate.value = str(exc)
                self._quote_ok = False

        await self._sync_approval(contract)
        if contract is None or amount <= 0 or not self._quote_ok:
            self.submit_button.disabled = True
        self.page.update()

    async def submit(self, contract: PoolContract) -> str:
        amount = self._lp_amount()
        if amount <= 0:
            raise WalletError("Enter an amount to withdraw.")
        if self.underlying:
            index = self._coin_index()
            expected = await contract.zap_calc_withdraw_one_coin(amount, index)
            return await contract.zap_remove_liquidity_one_coin(
                amount, index, self.with_slippage(expected)
            )
        if self.mode.value == "one":
            index = self._coin_index()
            expected = await contract.calc_withdraw_one_coin(amount, index)
            return await contract.remove_liquidity_one_coin(
                amount, index, self.with_slippage(expected)
            )
        # Balanced withdrawal: the floor is this LP amount's share of each
        # reserve, minus slippage. A zero floor would be simpler and is what
        # many UIs send, but it offers no protection at all against a
        # sandwich.
        #
        # The reserves come from the API (already scaled to human numbers)
        # and the divisor from the LP token on chain, because the pool's own
        # `balances` getter is `int128` on old pools and `uint256` on new
        # ones -- the same ABI split as the coin indices. Stale reserves only
        # matter to the extent they exceed the slippage tolerance.
        min_amounts = [0] * self.pool.n_coins
        try:
            supply = await contract.lp_total_supply()
        except WalletError:
            supply = 0
        if supply > 0:
            for index, coin in enumerate(self.pool.pool_coins):
                reserve = int(coin.balance * 10**coin.decimals)
                share = reserve * amount // supply
                min_amounts[index] = self.with_slippage(share) if share else 0
        return await contract.remove_liquidity(amount, min_amounts)


class SwapTab(ActionTab):
    """Exchange one coin for another inside this pool. No router."""

    title = "Swap"
    submit_label = "Swap"
    done_verb = "Swapped"
    #: `get_dy` is exact -- the same maths the swap itself runs, fee
    #: included -- so there is no estimator error to give back.
    fee_multiple = SLIPPAGE_OF_FEE
    slippage_constant = 0.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.balance = 0
        self._expected_out = 0
        # A metapool can swap its underlying coins itself -- it does the
        # base-pool leg internally -- so unlike depositing, this route
        # needs no zap and approves nothing but the pool. That is why it
        # is offered on chains where no zap was ever deployed.
        #: The pool for a StableSwap metapool, its zap for a crypto one,
        #: None where the route does not exist.
        self.underlying_spender = underlying_swap_spender(self.pool)
        self.route = _route_picker(
            self._route_changed, underlying=self.underlying_spender is not None
        )
        self.route.visible = self.underlying_spender is not None
        coins = self.coins
        self.from_coin = ft.Dropdown(
            label="From",
            options=_coin_options(coins, self.pool.chain),
            value="0",
            dense=True,
            expand=True,
            leading_icon=self._mark(0),
            on_select=self._from_selected,
        )
        to_index = 1 if self.pool.n_coins > 1 else 0
        self.to_coin = ft.Dropdown(
            label="To",
            options=_coin_options(coins, self.pool.chain),
            value=str(to_index),
            dense=True,
            expand=True,
            leading_icon=self._mark(to_index),
            on_select=self._to_selected,
        )
        # Two arrows rather than a word: this is the one control here that
        # is understood faster as a picture, and Material bundles the icon
        # font in both builds -- a "⇄" in the label font renders as tofu on
        # web, which is why the sort arrows are icons too.
        self.flip_button = ft.IconButton(
            ft.Icons.SWAP_HORIZ,
            tooltip="Swap the two coins over",
            on_click=self._flip,
        )
        # The amount is denominated in whatever "From" currently is, so its
        # mark follows the dropdown rather than being fixed at build time.
        self.amount = _amount_field(
            "Amount", self._changed, self._mark(0), on_max=self._max
        )
        self.balance_label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)

    @property
    def underlying(self) -> bool:
        return self.underlying_spender is not None and self.route.value == "underlying"

    @property
    def coins(self) -> list:
        return self.pool.display_coins if self.underlying else self.pool.pool_coins

    @property
    def spender(self) -> str:
        """Whoever moves the coin being sold on the live route."""
        if self.underlying and self.underlying_spender:
            return self.underlying_spender
        return self.pool.address

    def _route_changed(self, _e: ft.ControlEvent) -> None:
        """Swap both coin lists, and start from the top of the new one."""
        options = _coin_options(self.coins, self.pool.chain)
        self.from_coin.options = options
        self.to_coin.options = _coin_options(self.coins, self.pool.chain)
        self.from_coin.value = "0"
        self.to_coin.value = self._other_index(0)
        self._changed(None)

    def build(self) -> list[ft.Control]:
        return [
            self.route,
            ft.Row(
                [self.from_coin, self.flip_button, self.to_coin],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            _stacked(self.amount, self.balance_label),
        ]

    def _indices(self) -> tuple[int, int]:
        try:
            return int(self.from_coin.value or "0"), int(self.to_coin.value or "1")
        except ValueError:
            return 0, 1

    def summary(self) -> str:
        i, j = self._indices()
        coins = self.coins
        dx = self._dx()
        if dx <= 0 or i >= len(coins) or j >= len(coins):
            return ""
        return f"{self.amount_label(coins[i].address, dx)} for {coins[j].symbol}"

    async def fee_units(self, contract: PoolContract) -> int:
        """StableSwap-NG charges per pair, so ask about *this* pair.

        Not on the underlying route: those indices are into a list the
        pool does not price pairs for, and the trade crosses both pools
        anyway, so it is charged both fees.
        """
        i, j = self._indices()
        if not self.underlying:
            return await contract.pair_fee(i, j)
        fee = await contract.fee()
        try:
            return fee + await contract.base_fee()
        except WalletError:
            return fee

    def fee_key(self) -> object:
        return (self.route.value, *self._indices())

    def clear_inputs(self) -> None:
        self.amount.value = ""

    def _dx(self) -> int:
        i, _ = self._indices()
        text = (self.amount.value or "").strip()
        try:
            return parse_units(text, self.coins[i].decimals) if text else 0
        except (ValueError, IndexError):
            return 0

    def _max(self, _e: ft.ControlEvent) -> None:
        """Sell the whole balance of whichever coin is selected."""
        i, _ = self._indices()
        coin = self.coins[i]
        self.amount.value = format_units(
            self.balance, coin.decimals, precision=coin.decimals
        )
        self.page.run_task(self.refresh)

    def _mark(self, index: int, size: float = 20) -> ft.Control | None:
        """The mark for coin `index`, or None when there is no such coin."""
        coins = self.coins
        if not 0 <= index < len(coins):
            return None
        return token_mark(coins[index], self.pool.chain, size)

    def _other_index(self, taken: int) -> str:
        """Any coin but `taken`, for the side that has to give way."""
        for index in range(len(self.coins)):
            if index != taken:
                return str(index)
        return str(taken)

    def _from_selected(self, e: ft.ControlEvent) -> None:
        """Picking the coin the other side already holds moves that side.

        Rather than leaving the pair equal and printing "pick two different
        coins", which is a complaint about something the app can simply
        fix. The side that gives way is the one not just touched.
        """
        i, j = self._indices()
        if i == j:
            self.to_coin.value = self._other_index(i)
        self._changed(e)

    def _to_selected(self, e: ft.ControlEvent) -> None:
        i, j = self._indices()
        if i == j:
            self.from_coin.value = self._other_index(j)
        self._changed(e)

    def _flip(self, e: ft.ControlEvent) -> None:
        """Sell what you were buying. The amount stays as typed.

        It is denominated in whichever coin is on the left, so leaving the
        number alone means the field still says what it says -- and the
        quote below it is re-read either way.
        """
        self.from_coin.value, self.to_coin.value = (
            self.to_coin.value,
            self.from_coin.value,
        )
        self._changed(e)

    def _changed(self, _e: ft.ControlEvent) -> None:
        i, j = self._indices()
        # A control cannot be mounted twice, so each side gets its own mark.
        self.from_coin.leading_icon = self._mark(i)
        self.to_coin.leading_icon = self._mark(j)
        self.amount.prefix_icon = self._mark(i)
        self.page.run_task(self.refresh)

    async def refresh(self) -> None:
        contract = self.get_contract()
        if not await self.network_ok(contract):
            self.page.update()
            return
        i, j = self._indices()
        await self.suggest_slippage(contract)
        dx = self._dx()

        if contract is not None:
            try:
                self.balance = await contract.balance_of(self.coins[i].address)
                self.balance_label.value = (
                    f"Balance: {format_units(self.balance, self.coins[i].decimals)}"
                    f" {self.coins[i].symbol}"
                )
            except WalletError:
                self.balance_label.value = ""

        self._expected_out = 0
        self.estimate.value = ""
        if i == j:
            self.estimate.value = "Pick two different coins."
        elif contract is not None and dx > 0:
            try:
                self._expected_out = await (
                    contract.get_dy_underlying(i, j, dx)
                    if self.underlying
                    else contract.get_dy(i, j, dx)
                )
                out_coin = self.coins[j]
                self.estimate.value = (
                    f"~ {token_amount(units_to_float(self._expected_out, out_coin.decimals))}"
                    f" {out_coin.symbol}"
                    f"  (min {token_amount(units_to_float(self.with_slippage(self._expected_out), out_coin.decimals))})"
                )
            except WalletError as exc:
                self.estimate.value = str(exc)

        await self._sync_approval(contract)
        if contract is not None and (dx <= 0 or i == j):
            self.submit_button.disabled = True
        self.page.update()

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        i, _ = self._indices()
        dx = self._dx()
        if dx <= 0:
            return None
        coin = self.coins[i]
        spender = self.spender
        allowance = await contract.allowance(coin.address, spender)
        if allowance < dx:
            self.approve_button.content = f"1. Approve {coin.symbol}"
            return (coin.address, spender, dx)
        return None

    async def submit(self, contract: PoolContract) -> str:
        i, j = self._indices()
        dx = self._dx()
        if i == j:
            raise WalletError("Pick two different coins.")
        if dx <= 0:
            raise WalletError("Enter an amount to swap.")
        if self.underlying:
            expected = await contract.get_dy_underlying(i, j, dx)
            return await contract.exchange_underlying(
                i, j, dx, self.with_slippage(expected)
            )
        expected = await contract.get_dy(i, j, dx)
        return await contract.exchange(i, j, dx, self.with_slippage(expected))


class StakeTab(ActionTab):
    """Move LP tokens in and out of the pool's gauge."""

    title = "Stake"
    submit_label = "Stake"
    uses_slippage = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lp_balance = 0
        self.staked = 0
        self.amount = _amount_field(
            "LP tokens",
            self._changed,
            pool_stack(self.pool, 18, limit=4),
            on_max=self._max,
        )
        self.balances_label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.direction = ft.RadioGroup(
            value="stake",
            content=ft.Row(
                [
                    ft.Radio(value="stake", label="Stake"),
                    ft.Radio(value="unstake", label="Unstake"),
                ]
            ),
            on_change=self._changed,
        )

    @property
    def done_verb(self) -> str:
        """Which way the gauge went. Not a constant, like the others are."""
        return "Unstaked" if self.direction.value == "unstake" else "Staked"

    def summary(self) -> str:
        amount = self._amount_units()
        return self.amount_label(self.pool.lp_token, amount) if amount > 0 else ""

    def build(self) -> list[ft.Control]:
        if not self.pool.has_gauge:
            return [
                ft.Text(
                    "This pool has no gauge, so there is nothing to stake.",
                    size=SMALL,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
            ]
        return [
            self.direction,
            _stacked(self.amount, self.balances_label),
        ]

    def _max(self, _e: ft.ControlEvent) -> None:
        source = self.lp_balance if self.direction.value == "stake" else self.staked
        self.amount.value = format_units(source, 18, precision=18)
        self.page.run_task(self.refresh)

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh)

    def clear_inputs(self) -> None:
        self.amount.value = ""

    def _amount_units(self) -> int:
        text = (self.amount.value or "").strip()
        try:
            return parse_units(text, 18) if text else 0
        except ValueError:
            return 0

    async def refresh(self) -> None:
        contract = self.get_contract()
        if not self.pool.has_gauge:
            self.submit_button.visible = False
            self.approve_button.visible = False
            self.page.update()
            return
        if not await self.network_ok(contract):
            self.page.update()
            return

        if contract is not None:
            try:
                self.lp_balance = await contract.lp_balance()
                self.staked = await contract.staked_balance()
                self.balances_label.value = (
                    f"Wallet: {format_units(self.lp_balance, 18)} LP  ·  "
                    f"Staked: {format_units(self.staked, 18)} LP"
                )
            except WalletError:
                self.balances_label.value = ""

        staking = self.direction.value == "stake"
        self.submit_label = "Stake" if staking else "Unstake"
        self.submit_button.content = self.submit_label
        amount = self._amount_units()

        if staking:
            await self._sync_approval(contract)
        else:
            # Unstaking burns the gauge's own token; no allowance involved.
            self.approve_button.visible = False
            self.submit_button.disabled = contract is None
        if contract is not None and amount <= 0:
            self.submit_button.disabled = True
        self.page.update()

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        if self.direction.value != "stake":
            return None
        amount = self._amount_units()
        if amount <= 0:
            return None
        allowance = await contract.allowance(self.pool.lp_token, self.pool.gauge)
        if allowance < amount:
            self.approve_button.content = "1. Approve LP"
            return (self.pool.lp_token, self.pool.gauge, amount)
        return None

    async def submit(self, contract: PoolContract) -> str:
        amount = self._amount_units()
        if amount <= 0:
            raise WalletError("Enter an amount.")
        if self.direction.value == "stake":
            return await contract.stake(amount)
        return await contract.unstake(amount)
