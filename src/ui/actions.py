"""The four things you can do to a pool: deposit, withdraw, swap, stake.

Note every coin list here is `pool.pool_coins`, not `pool.coins`. On a
metapool those differ: v2 decomposes the base pool into `coins`, so a
two-coin metapool reports four. `add_liquidity` takes a `uint256[N]` whose
N is part of the function signature, so building it from the decomposed
list is calldata for a function the pool does not have.

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

from typing import Awaitable, Callable

import flet as ft

from curve.abi import FEE_DENOMINATOR, apply_slippage
from curve.confirm import POLL_INTERVAL, wait_for_confirmation
from curve.format import token_amount, units_to_float
from curve.models import Pool
from curve.pool import PoolContract
from wallet.base import WalletError

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

#: The deposit and withdrawal side, where the quote is not exact, as
#: `a * fee + b`. Both constants are measured, at one block, by asking the
#: pools rather than by reasoning about them.
#:
#: `a` -- a balanced deposit pays no imbalance fee, so splitting one into
#: its single-coin parts and quoting those separately reveals whether the
#: estimate charges: if it does, the parts come to less than the whole.
#: Across thirteen mainnet pools the gap is 0.500x the fee almost exactly
#: (PayPool, USDG, NUSD, RLUSD, weETH, YB-WETH 0.50; crvUSD pools 0.41-0.48;
#: the crypto pools 0.50 rising with size as price impact joins in), which
#: is what the arithmetic says a fully single-sided deposit should pay:
#: Curve's adjusted fee is base*N/(4(N-1)), applied to every coin's
#: distance from balance, and that sums to base/2.
#:
#: The exceptions are the ones that matter: **3pool (`main`) and stETH-ng
#: (old `factory`) show a gap of 0.00000%** -- their `calc_token_amount`
#: ignores fees entirely, so the mint comes in up to half a fee below the
#: quote. Half the fee therefore covers the worst implementation, and is
#: harmless margin on the rest.
ESTIMATE_FEE_SHARE = 0.5

#: `b` -- what the pool moves by between quoting and landing, independent
#: of any fee. Same basket quoted at the head and at 1, 2, 5, 25 and 100
#: blocks back: stable pools drift under 0.002% even over 20 minutes, and
#: the crypto pools up to 0.048% over 5 minutes (they track a price
#: oracle). At the one or two blocks a signature actually takes, every pool
#: measured 0.00000%. Two hundredths of a percent is comfortable there and
#: still small enough not to be worth sandwiching.
QUOTE_DRIFT = 0.02


def slippage_for(
    fee_units: int, multiple: float = SLIPPAGE_OF_FEE, constant: float = 0.0
) -> float:
    """`a * fee + b`, in percent, from a fee in Curve's 1e10 units."""
    return fee_units / FEE_DENOMINATOR * 100 * multiple + constant


def format_slippage(percent: float) -> str:
    """Three significant figures, which is as fine as the field is read."""
    return f"{percent:.3g}"


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
    #: Does this action have a price to protect? Staking does not -- it
    #: moves LP tokens into a gauge at no rate at all -- so showing a
    #: tolerance there invites someone to tune a number that does nothing.
    uses_slippage = True
    #: How the tolerance is derived from the pool fee. The default is the
    #: cautious one, for the actions quoted by `calc_token_amount`; a swap
    #: overrides it, because `get_dy` is exact.
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
        self.status = ft.Text("", size=SMALL, selectable=True)
        self.estimate = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)

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
            *self.build(),
            # Next to the amounts it protects. Down by the button it read
            # as part of the action rather than as a setting for it.
            *([_aside(self.slippage)] if self.uses_slippage else []),
            self.estimate,
            self.approve_button,
            self.submit_button,
            self.status,
        ]
        return self.control

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

    def _say(self, message: str, colour: str | None = None) -> None:
        self.status.value = message
        self.status.color = colour or ft.Colors.ON_SURFACE_VARIANT
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
        """
        self._say(f"Waiting for {tx[:14]}… to confirm.")
        block = await wait_for_confirmation(
            contract.provider, tx, interval=CONFIRM_INTERVAL
        )
        self._say(f"{done} (block {block:,})", ft.Colors.GREEN_600)

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
                self._say("Confirm the approval in your wallet…")
                tx = await contract.approve(token, spender, amount)
                await self._confirm(contract, tx, "Approved.")
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
            self._say("Confirm in your wallet…")
            tx = await self.submit(contract)
            await self._confirm(contract, tx, f"{self.title} confirmed.")
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
        self.submit_button.text = (
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
    """Add liquidity: an amount per coin, quoted as LP tokens out."""

    title = "Deposit"
    submit_label = "Deposit"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields: list[ft.TextField] = []
        self.balances: list[int] = [0] * self.pool.n_coins
        self.balance_labels: list[ft.Text] = []
        self._expected_lp = 0

    def build(self) -> list[ft.Control]:
        rows: list[ft.Control] = []
        for index, coin in enumerate(self.pool.pool_coins):
            field = _amount_field(
                coin.symbol,
                self._changed,
                token_mark(coin, self.pool.chain, 20),
                on_max=self._max_for(index),
            )
            label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
            self.fields.append(field)
            self.balance_labels.append(label)
            rows.append(_stacked(field, label))
        return rows

    def _max_for(self, index: int):
        """Fill one coin's field with the whole wallet balance.

        Full precision, not the shortened form the balance line shows:
        this is the number that becomes calldata, and a rounded one would
        either leave dust or exceed what is there.
        """

        def fill(_e: ft.ControlEvent) -> None:
            coin = self.pool.pool_coins[index]
            self.fields[index].value = format_units(
                self.balances[index], coin.decimals, precision=coin.decimals
            )
            self.page.run_task(self.refresh)

        return fill

    def clear_inputs(self) -> None:
        for field in self.fields:
            field.value = ""

    def _amounts(self) -> list[int]:
        out = []
        for field, coin in zip(self.fields, self.pool.pool_coins):
            text = (field.value or "").strip()
            try:
                out.append(parse_units(text, coin.decimals) if text else 0)
            except ValueError:
                out.append(0)
        return out

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh)

    async def refresh(self) -> None:
        contract = self.get_contract()
        await self.suggest_slippage(contract)
        if contract is not None:
            for index, coin in enumerate(self.pool.pool_coins):
                try:
                    self.balances[index] = await contract.balance_of(coin.address)
                except WalletError:
                    self.balances[index] = 0
                self.balance_labels[index].value = (
                    f"Balance: {format_units(self.balances[index], coin.decimals)}"
                )

        amounts = self._amounts()
        self._expected_lp = 0
        if contract is not None and any(amounts):
            try:
                self._expected_lp = await contract.calc_token_amount(amounts, deposit=True)
                self.estimate.value = (
                    f"~ {token_amount(units_to_float(self._expected_lp, 18))} LP"
                    f"  (min {token_amount(units_to_float(self.with_slippage(self._expected_lp), 18))})"
                )
            except WalletError as exc:
                self.estimate.value = str(exc)
        else:
            self.estimate.value = ""

        await self._sync_approval(contract)
        if contract is not None and not any(amounts):
            self.submit_button.disabled = True
        self.page.update()

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        # One coin at a time: each ERC-20 needs its own approval, and the UI
        # walks them in order rather than batching, so the button always
        # names a single concrete step.
        for coin, amount in zip(self.pool.pool_coins, self._amounts()):
            if amount <= 0:
                continue
            allowance = await contract.allowance(coin.address, self.pool.address)
            if allowance < amount:
                self.approve_button.text = f"1. Approve {coin.symbol}"
                return (coin.address, self.pool.address, amount)
        return None

    async def submit(self, contract: PoolContract) -> str:
        amounts = self._amounts()
        if not any(amounts):
            raise WalletError("Enter an amount to deposit.")
        expected = await contract.calc_token_amount(amounts, deposit=True)
        return await contract.add_liquidity(amounts, self.with_slippage(expected))


class WithdrawTab(ActionTab):
    """Remove liquidity, either balanced or all into one coin."""

    title = "Withdraw"
    submit_label = "Withdraw"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lp_balance = 0
        # An LP token has no logo of its own; Curve draws the pool's coins.
        self.amount = _amount_field(
            "LP tokens",
            self._changed,
            pool_stack(self.pool, 18, limit=4),
            on_max=self._max,
        )
        self.lp_label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.mode = ft.RadioGroup(
            value="balanced",
            content=ft.Row(
                [
                    ft.Radio(value="balanced", label="All coins"),
                    ft.Radio(value="one", label="One coin"),
                ]
            ),
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

    def build(self) -> list[ft.Control]:
        return [
            _stacked(self.amount, self.lp_label),
            self.mode,
            self.coin_picker,
        ]

    def _max(self, _e: ft.ControlEvent) -> None:
        self.amount.value = format_units(self.lp_balance, 18, precision=18)
        self.page.run_task(self.refresh)

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.coin_picker.visible = self.mode.value == "one"
        coins = self.pool.pool_coins
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

    async def refresh(self) -> None:
        contract = self.get_contract()
        await self.suggest_slippage(contract)
        amount = self._lp_amount()
        if contract is not None:
            try:
                self.lp_balance = await contract.lp_balance()
                self.lp_label.value = f"Balance: {format_units(self.lp_balance, 18)} LP"
            except WalletError:
                self.lp_label.value = ""

        self.estimate.value = ""
        if contract is not None and amount > 0 and self.mode.value == "one":
            index = self._coin_index()
            coin = self.pool.pool_coins[index]
            try:
                out = await contract.calc_withdraw_one_coin(amount, index)
                self.estimate.value = (
                    f"~ {token_amount(units_to_float(out, coin.decimals))} {coin.symbol}"
                    f"  (min {token_amount(units_to_float(self.with_slippage(out), coin.decimals))})"
                )
            except WalletError as exc:
                self.estimate.value = str(exc)

        # Burning LP needs no approval: the pool burns the caller's own
        # balance rather than transferring it, so there is no spender.
        self.approve_button.visible = False
        self.submit_button.text = self.submit_label
        self.submit_button.disabled = contract is None or amount <= 0
        self.page.update()

    async def submit(self, contract: PoolContract) -> str:
        amount = self._lp_amount()
        if amount <= 0:
            raise WalletError("Enter an amount to withdraw.")
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
    #: `get_dy` is exact -- it is the same maths the swap itself runs, fee
    #: included -- so there is no estimator error to give back.
    fee_multiple = SLIPPAGE_OF_FEE
    slippage_constant = 0.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.balance = 0
        self._expected_out = 0
        coins = self.pool.pool_coins
        self.from_coin = ft.Dropdown(
            label="From",
            options=_coin_options(coins, self.pool.chain),
            value="0",
            dense=True,
            expand=True,
            leading_icon=self._mark(0),
            on_select=self._changed,
        )
        to_index = 1 if self.pool.n_coins > 1 else 0
        self.to_coin = ft.Dropdown(
            label="To",
            options=_coin_options(coins, self.pool.chain),
            value=str(to_index),
            dense=True,
            expand=True,
            leading_icon=self._mark(to_index),
            on_select=self._changed,
        )
        # The amount is denominated in whatever "From" currently is, so its
        # mark follows the dropdown rather than being fixed at build time.
        self.amount = _amount_field(
            "Amount", self._changed, self._mark(0), on_max=self._max
        )
        self.balance_label = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)

    def build(self) -> list[ft.Control]:
        return [
            ft.Row([self.from_coin, self.to_coin], spacing=8),
            _stacked(self.amount, self.balance_label),
        ]

    def _indices(self) -> tuple[int, int]:
        try:
            return int(self.from_coin.value or "0"), int(self.to_coin.value or "1")
        except ValueError:
            return 0, 1

    async def fee_units(self, contract: PoolContract) -> int:
        """StableSwap-NG charges per pair, so ask about *this* pair."""
        i, j = self._indices()
        return await contract.pair_fee(i, j)

    def fee_key(self) -> object:
        return self._indices()

    def clear_inputs(self) -> None:
        self.amount.value = ""

    def _dx(self) -> int:
        i, _ = self._indices()
        text = (self.amount.value or "").strip()
        try:
            return parse_units(text, self.pool.pool_coins[i].decimals) if text else 0
        except (ValueError, IndexError):
            return 0

    def _max(self, _e: ft.ControlEvent) -> None:
        """Sell the whole balance of whichever coin is selected."""
        i, _ = self._indices()
        coin = self.pool.pool_coins[i]
        self.amount.value = format_units(
            self.balance, coin.decimals, precision=coin.decimals
        )
        self.page.run_task(self.refresh)

    def _mark(self, index: int, size: float = 20) -> ft.Control | None:
        """The mark for coin `index`, or None when there is no such coin."""
        coins = self.pool.pool_coins
        if not 0 <= index < len(coins):
            return None
        return token_mark(coins[index], self.pool.chain, size)

    def _changed(self, _e: ft.ControlEvent) -> None:
        i, j = self._indices()
        # A control cannot be mounted twice, so each side gets its own mark.
        self.from_coin.leading_icon = self._mark(i)
        self.to_coin.leading_icon = self._mark(j)
        self.amount.prefix_icon = self._mark(i)
        self.page.run_task(self.refresh)

    async def refresh(self) -> None:
        contract = self.get_contract()
        i, j = self._indices()
        await self.suggest_slippage(contract)
        dx = self._dx()

        if contract is not None:
            try:
                self.balance = await contract.balance_of(self.pool.pool_coins[i].address)
                self.balance_label.value = (
                    f"Balance: {format_units(self.balance, self.pool.pool_coins[i].decimals)}"
                    f" {self.pool.pool_coins[i].symbol}"
                )
            except WalletError:
                self.balance_label.value = ""

        self._expected_out = 0
        self.estimate.value = ""
        if i == j:
            self.estimate.value = "Pick two different coins."
        elif contract is not None and dx > 0:
            try:
                self._expected_out = await contract.get_dy(i, j, dx)
                out_coin = self.pool.pool_coins[j]
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
        coin = self.pool.pool_coins[i]
        allowance = await contract.allowance(coin.address, self.pool.address)
        if allowance < dx:
            self.approve_button.text = f"1. Approve {coin.symbol}"
            return (coin.address, self.pool.address, dx)
        return None

    async def submit(self, contract: PoolContract) -> str:
        i, j = self._indices()
        dx = self._dx()
        if i == j:
            raise WalletError("Pick two different coins.")
        if dx <= 0:
            raise WalletError("Enter an amount to swap.")
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
        self.submit_button.text = "Stake" if staking else "Unstake"
        self.submit_label = self.submit_button.text
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
            self.approve_button.text = "1. Approve LP"
            return (self.pool.lp_token, self.pool.gauge, amount)
        return None

    async def submit(self, contract: PoolContract) -> str:
        amount = self._amount_units()
        if amount <= 0:
            raise WalletError("Enter an amount.")
        if self.direction.value == "stake":
            return await contract.stake(amount)
        return await contract.unstake(amount)
