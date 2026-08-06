"""Talking to a pool on-chain, through whatever wallet is connected.

Everything the action panels need -- quotes, allowances, balances, and the
five transactions (exchange, deposit, withdraw, stake, unstake) -- expressed
against `wallet.WalletProvider`. That provider proxies `eth_call` to the
user's own node, so this file needs no RPC URL of its own.

The one non-obvious rule in here is `_read`, and it is worth stating up
front because getting it wrong is silent:

> Calling a function a Curve pool does not implement returns **empty data**,
> not an error. `decode_uint("0x")` is 0, so a mis-typed pool would quote
> every swap at zero output instead of failing.

That is not hypothetical -- it is exactly what a StableSwap-signature
`get_dy` does against a CryptoSwap pool, confirmed against mainnet. So every
read here rejects empty return data.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallet.base import RpcError, WalletError, WalletProvider  # noqa: E402

from . import abi  # noqa: E402
from .models import Pool  # noqa: E402


class PoolCallFailed(WalletError):
    """A pool read returned nothing usable."""


class PoolContract:
    """One pool, bound to a connected wallet."""

    def __init__(self, provider: WalletProvider, pool: Pool, account: str) -> None:
        self.provider = provider
        self.pool = pool
        self.account = account

    # -- reads ------------------------------------------------------------

    async def _read(self, to: str, data: str, what: str) -> int:
        """`eth_call` returning one uint256, with the empty-data guard."""
        try:
            result = await self.provider.call(to, data)
        except RpcError as exc:
            raise PoolCallFailed(f"Could not read {what}: {exc.message}") from exc
        if not result or result in ("0x", "0x0"):
            # See the module docstring: this is what a wrong signature looks
            # like, and it must never be mistaken for a legitimate zero.
            raise PoolCallFailed(
                f"The pool did not answer {what} — it may not support this action."
            )
        return abi.decode_uint(result)

    async def get_dy(self, i: int, j: int, dx: int) -> int:
        """Quote an in-pool swap of `dx` units of coin `i` into coin `j`."""
        if dx <= 0:
            return 0
        return await self._read(
            self.pool.address,
            abi.encode_get_dy(i, j, dx, stableswap=self.pool.is_stableswap),
            "the exchange rate",
        )

    async def calc_token_amount(self, amounts: list[int], *, deposit: bool = True) -> int:
        """Estimate LP tokens minted (or burned) for a set of coin amounts.

        Tries the `(amounts, bool)` spelling first and falls back to the
        older CryptoSwap `(amounts)` one. The two cannot be distinguished
        from API metadata, and the failure mode is empty data rather than a
        revert, so both paths go through `_read`.
        """
        if not any(amounts):
            return 0
        try:
            return await self._read(
                self.pool.address,
                abi.encode_calc_token_amount(amounts, deposit=deposit),
                "the deposit estimate",
            )
        except PoolCallFailed:
            return await self._read(
                self.pool.address,
                abi.encode_calc_token_amount_no_flag(amounts),
                "the deposit estimate",
            )

    async def calc_withdraw_one_coin(self, lp_amount: int, i: int) -> int:
        if lp_amount <= 0:
            return 0
        return await self._read(
            self.pool.address,
            abi.encode_calc_withdraw_one_coin(
                lp_amount, i, stableswap=self.pool.is_stableswap
            ),
            "the withdrawal estimate",
        )

    async def balance_of(self, token: str, owner: str | None = None) -> int:
        """ERC-20 balance. Zero is a legitimate answer here, unlike a quote."""
        from wallet.erc20 import encode_balance_of  # noqa: PLC0415

        try:
            result = await self.provider.call(
                token, encode_balance_of(owner or self.account)
            )
        except RpcError as exc:
            raise PoolCallFailed(f"Could not read balance: {exc.message}") from exc
        return abi.decode_uint(result)

    async def lp_balance(self, owner: str | None = None) -> int:
        return await self.balance_of(self.pool.lp_token, owner)

    async def lp_total_supply(self) -> int:
        """LP tokens outstanding, for pricing a balanced withdrawal."""
        try:
            result = await self.provider.call(
                self.pool.lp_token, abi.encode_total_supply()
            )
        except RpcError as exc:
            raise PoolCallFailed(f"Could not read LP supply: {exc.message}") from exc
        return abi.decode_uint(result)

    async def staked_balance(self, owner: str | None = None) -> int:
        """LP tokens held in the gauge. A gauge is itself an ERC-20."""
        if not self.pool.has_gauge:
            return 0
        return await self.balance_of(self.pool.gauge, owner)

    async def allowance(self, token: str, spender: str) -> int:
        try:
            result = await self.provider.call(
                token, abi.encode_allowance(self.account, spender)
            )
        except RpcError as exc:
            raise PoolCallFailed(f"Could not read allowance: {exc.message}") from exc
        return abi.decode_uint(result)

    # -- writes -----------------------------------------------------------
    #
    # Gas and nonce are deliberately left unset on every transaction: the
    # wallet fills them in and knows the chain better than this app does.

    async def _send(self, to: str, data: str) -> str:
        return await self.provider.send_transaction(
            {"from": self.account, "to": to, "value": "0x0", "data": data}
        )

    async def approve(self, token: str, spender: str, amount: int) -> str:
        """Approve exactly `amount` rather than an unlimited allowance.

        An infinite approval is one signature cheaper over time, but it
        leaves the pool able to move the user's whole balance forever. For
        an app whose point is to demonstrate the flow, the safer default is
        the honest one.
        """
        return await self._send(token, abi.encode_approve(spender, amount))

    async def exchange(self, i: int, j: int, dx: int, min_dy: int) -> str:
        return await self._send(
            self.pool.address,
            abi.encode_exchange(i, j, dx, min_dy, stableswap=self.pool.is_stableswap),
        )

    async def add_liquidity(self, amounts: list[int], min_mint: int) -> str:
        return await self._send(
            self.pool.address, abi.encode_add_liquidity(amounts, min_mint)
        )

    async def remove_liquidity(self, lp_amount: int, min_amounts: list[int]) -> str:
        return await self._send(
            self.pool.address, abi.encode_remove_liquidity(lp_amount, min_amounts)
        )

    async def remove_liquidity_one_coin(
        self, lp_amount: int, i: int, min_amount: int
    ) -> str:
        return await self._send(
            self.pool.address,
            abi.encode_remove_liquidity_one_coin(
                lp_amount, i, min_amount, stableswap=self.pool.is_stableswap
            ),
        )

    async def stake(self, amount: int) -> str:
        if not self.pool.has_gauge:
            raise PoolCallFailed("This pool has no gauge to stake in.")
        return await self._send(self.pool.gauge, abi.encode_gauge_deposit(amount))

    async def unstake(self, amount: int) -> str:
        if not self.pool.has_gauge:
            raise PoolCallFailed("This pool has no gauge to unstake from.")
        return await self._send(self.pool.gauge, abi.encode_gauge_withdraw(amount))
