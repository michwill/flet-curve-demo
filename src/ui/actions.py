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

from curve.abi import apply_slippage
from curve.format import token_amount, units_to_float
from curve.models import Pool
from curve.pool import PoolContract
from wallet.base import WalletError
from wallet.erc20 import format_units, parse_units

#: Default tolerance. Curve shows 0.03% on pegged stable pools, but this app
#: applies one number to every pool type, so it errs toward the volatile end.
DEFAULT_SLIPPAGE = 0.5


class ActionTab:
    """Base for the four panels. Subclasses build fields and submit."""

    title = ""
    #: Label for the button that sends the main transaction.
    submit_label = "Confirm"

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

        self.slippage = ft.TextField(
            label="Slippage %",
            value=str(DEFAULT_SLIPPAGE),
            width=110,
            dense=True,
        )
        self.status = ft.Text("", size=12, selectable=True)
        self.estimate = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)

        self.approve_button = ft.Button(
            "1. Approve", on_click=self._approve_clicked, visible=False, disabled=True
        )
        self.submit_button = ft.Button(
            self.submit_label, on_click=self._submit_clicked, disabled=True
        )
        self.control = ft.Column(spacing=12)

    # -- to implement -----------------------------------------------------

    def build(self) -> list[ft.Control]:
        raise NotImplementedError

    async def refresh(self) -> None:
        """Re-read balances and re-quote. Must tolerate no wallet."""

    async def submit(self, contract: PoolContract) -> str:
        raise NotImplementedError

    async def approval_needed(self, contract: PoolContract) -> tuple[str, str, int] | None:
        """Return `(token, spender, amount)` still needing an allowance."""
        return None

    # -- shared behaviour -------------------------------------------------

    def mount(self) -> ft.Column:
        self.control.controls = [
            *self.build(),
            self.estimate,
            ft.Row([self.slippage], alignment=ft.MainAxisAlignment.END),
            self.approve_button,
            self.submit_button,
            self.status,
        ]
        return self.control

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
                self._say(f"Approved. {tx[:14]}…", ft.Colors.GREEN_600)
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
            self._say(f"Submitted: {tx}", ft.Colors.GREEN_600)
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


def _amount_field(label: str, on_change) -> ft.TextField:
    return ft.TextField(label=label, hint_text="0.0", dense=True, on_change=on_change)


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
        for coin in self.pool.pool_coins:
            field = _amount_field(coin.symbol, self._changed)
            label = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
            self.fields.append(field)
            self.balance_labels.append(label)
            rows.append(ft.Column([field, label], spacing=2))
        return rows

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
        self.amount = _amount_field("LP tokens", self._changed)
        self.lp_label = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
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
            options=[
                ft.DropdownOption(key=str(i), text=c.symbol)
                for i, c in enumerate(self.pool.pool_coins)
            ],
            value="0",
            dense=True,
            visible=False,
            on_select=self._changed,
        )

    def build(self) -> list[ft.Control]:
        return [
            ft.Column([self.amount, self.lp_label], spacing=2),
            self.mode,
            self.coin_picker,
            ft.TextButton("Max", on_click=self._max),
        ]

    def _max(self, _e: ft.ControlEvent) -> None:
        self.amount.value = format_units(self.lp_balance, 18, precision=18)
        self.page.run_task(self.refresh)

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.coin_picker.visible = self.mode.value == "one"
        self.page.run_task(self.refresh)

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
        # Balanced withdrawal: the floor is each coin's share of the pool,
        # taken from the reserves the API already gave us, minus slippage.
        # A zero floor would be simpler and is what many UIs send, but it
        # offers no protection at all against a sandwich.
        total_supply = sum(c.balance for c in self.pool.pool_coins)
        min_amounts = []
        for coin in self.pool.pool_coins:
            share = 0
            if self.lp_balance and total_supply:
                share = coin.pool_balance * amount // max(total_supply, 1)
            min_amounts.append(self.with_slippage(share) if share else 0)
        return await contract.remove_liquidity(amount, min_amounts)


class SwapTab(ActionTab):
    """Exchange one coin for another inside this pool. No router."""

    title = "Swap"
    submit_label = "Swap"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.balance = 0
        self._expected_out = 0
        options = [
            ft.DropdownOption(key=str(i), text=c.symbol)
            for i, c in enumerate(self.pool.pool_coins)
        ]
        self.from_coin = ft.Dropdown(
            label="From",
            options=list(options),
            value="0",
            dense=True,
            expand=True,
            on_select=self._changed,
        )
        self.to_coin = ft.Dropdown(
            label="To",
            options=list(options),
            value="1" if self.pool.n_coins > 1 else "0",
            dense=True,
            expand=True,
            on_select=self._changed,
        )
        self.amount = _amount_field("Amount", self._changed)
        self.balance_label = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)

    def build(self) -> list[ft.Control]:
        return [
            ft.Row([self.from_coin, self.to_coin], spacing=8),
            ft.Column([self.amount, self.balance_label], spacing=2),
        ]

    def _indices(self) -> tuple[int, int]:
        try:
            return int(self.from_coin.value or "0"), int(self.to_coin.value or "1")
        except ValueError:
            return 0, 1

    def _dx(self) -> int:
        i, _ = self._indices()
        text = (self.amount.value or "").strip()
        try:
            return parse_units(text, self.pool.pool_coins[i].decimals) if text else 0
        except (ValueError, IndexError):
            return 0

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh)

    async def refresh(self) -> None:
        contract = self.get_contract()
        i, j = self._indices()
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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lp_balance = 0
        self.staked = 0
        self.amount = _amount_field("LP tokens", self._changed)
        self.balances_label = ft.Text("", size=11, color=ft.Colors.ON_SURFACE_VARIANT)
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
                    size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
            ]
        return [
            self.direction,
            ft.Column([self.amount, self.balances_label], spacing=2),
            ft.TextButton("Max", on_click=self._max),
        ]

    def _max(self, _e: ft.ControlEvent) -> None:
        source = self.lp_balance if self.direction.value == "stake" else self.staked
        self.amount.value = format_units(source, 18, precision=18)
        self.page.run_task(self.refresh)

    def _changed(self, _e: ft.ControlEvent) -> None:
        self.page.run_task(self.refresh)

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
