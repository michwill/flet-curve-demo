"""Talking to a pool on-chain, through whatever wallet is connected.

Everything the action panels need -- quotes, allowances, balances, and the
transactions (exchange, deposit, withdraw, stake, unstake, and the combined
deposit-and-stake) -- expressed against `wallet.WalletProvider`. That
provider proxies `eth_call` to the user's own node, so this file needs no
RPC URL of its own.

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

import contextlib
import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallet.base import RpcError, WalletError, WalletProvider

from . import abi
from .models import Pool
from .multicall import MULTICALL3, decode_uints, encode_aggregate3
from .parameters import PARAMETERS
from .stake_zaps import ZERO_ADDRESS, StakeZap, stake_zap_for
from .zaps import Zap, zap_for

#: The two parameters that are spelled two ways: a tricrypto pool holds
#: several prices and takes an index, twocrypto and the stable factories
#: hold one and take none. The registry does not say which, so both are
#: asked -- which costs nothing now that they go in one batch. Index 0 is
#: the first priced coin, the one a single line of a summary should show.
INDEXED_PARAMETERS = ("price_oracle", "price_scale")


def _parameter_plan() -> list[tuple[str, str]]:
    """What to ask the pool, as `(key, calldata)`, bare spellings first."""
    plan = [(parameter.key, abi.encode_parameter(parameter.key)) for parameter in PARAMETERS]
    plan += [(key, abi.encode_indexed_parameter(key, 0)) for key in INDEXED_PARAMETERS]
    return plan


class PoolCallFailed(WalletError):
    """A pool read returned nothing usable."""


class PoolContract:
    """One pool, bound to a connected wallet."""

    def __init__(self, provider: WalletProvider, pool: Pool, account: str) -> None:
        self.provider = provider
        self.pool = pool
        #: Empty when the contract is bound to a public node rather than a
        #: wallet: quotes work, nothing else does. See `curve.rpc`.
        self.account = account
        #: The deposit zap for this pool's underlying coins, if there is
        #: one. None for every pool that is not a factory metapool, which
        #: is nearly all of them.
        self.zap: Zap | None = zap_for(pool)
        #: The deposit-and-stake zap for this *chain*, if the pool has a
        #: gauge to stake into. Unrelated to `zap` above: that one is about
        #: which coins a deposit may be made in, this one about whether the
        #: deposit and the stake can share a transaction.
        self.stake_zap: StakeZap | None = stake_zap_for(pool)

    @property
    def can_send(self) -> bool:
        """Is there an account behind this, or only a node?

        A quote needs neither an account nor a signature, so the panels
        show rates with no wallet connected; everything that moves a token
        needs both, and asks this first.
        """
        return bool(self.account)

    # -- reads ------------------------------------------------------------

    async def _read(
        self, to: str, data: str, what: str, subject: str = "The pool"
    ) -> int:
        """`eth_call` returning one uint256, with the empty-data guard.

        `subject` names whatever was asked, because a read on the
        underlying route goes to the zap and "the pool did not answer"
        would point at the wrong contract.
        """
        try:
            result = await self.provider.call(to, data)
        except RpcError as exc:
            raise PoolCallFailed(f"Could not read {what}: {exc.message}") from exc
        if not result or result in ("0x", "0x0"):
            # See the module docstring: this is what a wrong signature looks
            # like, and it must never be mistaken for a legitimate zero.
            raise PoolCallFailed(
                f"{subject} did not answer {what} — it may not support this action."
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

    def underlying_swap_target(self) -> tuple[str, bool]:
        """Who performs an underlying swap, and in which index width.

        A StableSwap metapool does it itself: `exchange_underlying` is on
        the pool, which does the base-pool leg internally, so nothing but
        the pool is ever approved. A *crypto* metapool has no such
        function at all -- confirmed against the chain -- and its per-pool
        zap carries one instead, with `uint256` indices. That zap is then
        the spender, which is the one place the two routes differ beyond
        the address.
        """
        if self.pool.is_stableswap:
            return self.pool.address, True
        zap = self.zap
        if zap is None or not zap.swaps:
            raise PoolCallFailed(
                "This pool cannot swap its underlying coins: it has no "
                "`exchange_underlying`, and no zap that does."
            )
        return zap.address, False

    async def get_dy_underlying(self, i: int, j: int, dx: int) -> int:
        """Quote a swap between two of a metapool's underlying coins."""
        if dx <= 0:
            return 0
        to, stableswap = self.underlying_swap_target()
        return await self._read(
            to,
            abi.encode_get_dy_underlying(i, j, dx, stableswap=stableswap),
            "the exchange rate",
            "The pool" if stableswap else "The zap",
        )

    async def calc_token_amount(self, amounts: list[int], *, deposit: bool = True) -> int:
        """Estimate LP tokens minted (or burned) for a set of coin amounts.

        Three spellings exist and the pool answers exactly one:

          * `uint256[N]` plus a flag -- the classic pools;
          * `uint256[]` plus a flag -- StableSwap-NG, whose amounts are a
            Vyper `DynArray`;
          * `uint256[N]` alone -- the oldest CryptoSwap pools.

        The registry says which to expect, and this asks anyway: an
        unknown factory would otherwise get calldata that reverts. The
        shape that answers is remembered on the pool, because
        `add_liquidity` has to send the same one and a transaction gets no
        second attempt.
        """
        if not any(amounts):
            return 0
        expected = self.pool.dynamic_arrays
        for dynamic in (expected, not expected):
            try:
                value = await self._read(
                    self.pool.address,
                    abi.encode_calc_token_amount(
                        amounts, deposit=deposit, dynamic=dynamic
                    ),
                    "the deposit estimate",
                )
            except PoolCallFailed:
                continue
            self.pool.observed_dynamic = dynamic
            return value
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

    async def fee(self) -> int:
        """The pool's swap fee, in Curve's 1e10 units."""
        return await self._read(self.pool.address, abi.encode_fee(), "the pool fee")

    async def parameters(self) -> dict[str, int]:
        """Every curve parameter this pool will answer, raw and unscaled.

        A pool that does not implement one is the normal case, not a
        failure: `gamma` on a StableSwap pool, `offpeg_fee_multiplier` on
        anything but StableSwap-NG. Those are left out rather than
        reported, so the caller shows what exists.

        Eleven questions -- nine parameters, and a second spelling for the
        two that have one -- asked in a **single** `eth_call` through
        Multicall3, which is why `curve.multicall` exists. Sequentially
        that is eleven round trips for one panel, and on a public endpoint
        eleven chances to be rate-limited.
        """
        plan = _parameter_plan()
        answers = await self._read_many(plan)
        found: dict[str, int] = {}
        for (key, _data), value in zip(plan, answers, strict=True):
            # First spelling that answers wins, and the plan lists the
            # bare form before the indexed one.
            if value is not None and key not in found:
                found[key] = value
        return found

    async def _read_many(self, plan: list[tuple[str, str]]) -> list[int | None]:
        """The batch, or one call each where there is no batching.

        Multicall3 is at the same address on every chain that has it, and
        a chain without it is a normal case -- a Lite deployment on a new
        chain may well predate it. There is no way to ask whether it is
        there that is cheaper than trying, and nothing distinguishes "no
        multicall" from "the batch answered nothing", so an empty answer
        falls back to asking one at a time.
        """
        with contextlib.suppress(Exception):
            result = await self.provider.call(
                MULTICALL3,
                encode_aggregate3([(self.pool.address, data) for _key, data in plan]),
            )
            values = decode_uints(result, len(plan))
            if any(value is not None for value in values):
                return values
        return [await self._maybe(data) for _key, data in plan]

    async def _maybe(self, data: str) -> int | None:
        """One read, where not being implemented is an expected answer.

        `PoolCallFailed` only, deliberately. It is the pool's answer --
        a revert, or the empty data a Vyper contract returns for a method
        it does not have -- and "this pool has no `gamma`" is a fact worth
        recording as absence.

        Any other `WalletError` is not the pool talking, it is that we
        never reached it: no node known for the chain, no node that
        answered. Swallowing those to `None` made a dead provider
        indistinguishable from a pool with no parameters, and the panel
        said "This pool answered none of them" -- which reads as a fact
        about the pool -- while the real trouble was that nothing had been
        asked. That is how the iOS breakage in `USER_AGENT` stayed hidden.
        So it propagates, and `load_parameters` prints the sentence.
        """
        try:
            return await self._read(self.pool.address, data, "a pool parameter")
        except PoolCallFailed:
            return None

    async def pair_fee(self, i: int, j: int) -> int:
        """The fee that applies to swapping `i` for `j`.

        StableSwap-NG prices each pair separately and exposes
        `dynamic_fee`; every other pool has one flat `fee()`. Not having
        the method looks like a revert on older Vyper and like empty data
        on newer, and both arrive here as `PoolCallFailed` -- so the
        fallback covers both without asking what the pool is.
        """
        try:
            return await self._read(
                self.pool.address, abi.encode_dynamic_fee(i, j), "the pair fee"
            )
        except PoolCallFailed:
            return await self.fee()

    async def base_fee(self) -> int:
        """The base pool's fee, for a deposit that passes through it.

        A zap deposit of an underlying coin is two deposits -- into the
        base pool, then into the metapool -- and pays both fees. Measured
        on a fork across the Ethereum metapools, the shortfall against the
        zap's own quote lands under their sum in every realistic case:
        0.045% against 0.055% on the 3pool metapools, and exactly zero on
        the NG ones, whose quote is fee-inclusive.
        """
        if not self.pool.base_pool:
            return 0
        return await self._read(
            self.pool.base_pool, abi.encode_fee(), "the base pool fee", "The base pool"
        )

    # -- the underlying route ---------------------------------------------
    #
    # Same five operations, addressed to the zap and carrying the pool as
    # their first argument. The amounts are the *underlying* coins --
    # `Pool.display_coins` -- rather than the two the contract holds.

    def _zap(self) -> Zap:
        if self.zap is None:
            raise PoolCallFailed(
                "This pool has no deposit zap, so its underlying coins "
                "cannot be deposited directly."
            )
        return self.zap

    def _zap_pool(self, zap: Zap) -> str | None:
        """The pool argument, where the zap takes one.

        A per-pool zap does not: it was deployed for this pool alone, and
        its calldata is what the pool itself would take.
        """
        return self.pool.address if zap.pool_arg else None

    async def zap_calc_token_amount(
        self, amounts: list[int], *, deposit: bool = True
    ) -> int:
        if not any(amounts):
            return 0
        zap = self._zap()
        return await self._read(
            zap.address,
            abi.encode_zap_calc_token_amount(
                self._zap_pool(zap),
                amounts,
                deposit=deposit,
                dynamic=zap.dynamic,
                stableswap=zap.stableswap,
            ),
            "the deposit estimate",
            "The deposit zap",
        )

    async def zap_calc_withdraw_one_coin(self, lp_amount: int, i: int) -> int:
        if lp_amount <= 0:
            return 0
        zap = self._zap()
        return await self._read(
            zap.address,
            abi.encode_zap_calc_withdraw_one_coin(
                self._zap_pool(zap), lp_amount, i, stableswap=zap.stableswap
            ),
            "the withdrawal estimate",
            "The deposit zap",
        )

    async def zap_add_liquidity(self, amounts: list[int], min_mint: int) -> str:
        zap = self._zap()
        return await self._send(
            zap.address,
            abi.encode_zap_add_liquidity(
                self._zap_pool(zap), amounts, min_mint, dynamic=zap.dynamic
            ),
        )

    async def zap_remove_liquidity_one_coin(
        self, lp_amount: int, i: int, min_amount: int
    ) -> str:
        zap = self._zap()
        return await self._send(
            zap.address,
            abi.encode_zap_remove_liquidity_one_coin(
                self._zap_pool(zap), lp_amount, i, min_amount, stableswap=zap.stableswap
            ),
        )

    async def zap_remove_liquidity(
        self, lp_amount: int, min_amounts: list[int]
    ) -> str:
        zap = self._zap()
        return await self._send(
            zap.address,
            abi.encode_zap_remove_liquidity(
                self._zap_pool(zap), lp_amount, min_amounts, dynamic=zap.dynamic
            ),
        )

    async def balance_of(self, token: str, owner: str | None = None) -> int:
        """ERC-20 balance. Zero is a legitimate answer here, unlike a quote."""
        from wallet.erc20 import encode_balance_of

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

    async def exchange_underlying(self, i: int, j: int, dx: int, min_dy: int) -> str:
        to, stableswap = self.underlying_swap_target()
        return await self._send(
            to, abi.encode_exchange_underlying(i, j, dx, min_dy, stableswap=stableswap)
        )

    async def add_liquidity(self, amounts: list[int], min_mint: int) -> str:
        """Deposit, in whichever array shape this pool speaks.

        The shape is whatever `calc_token_amount` proved -- the panel
        always quotes before it sends -- falling back to what the registry
        implies for a pool nobody has quoted yet.
        """
        return await self._send(
            self.pool.address,
            abi.encode_add_liquidity(
                amounts, min_mint, dynamic=self.pool.dynamic_arrays
            ),
        )

    async def remove_liquidity(self, lp_amount: int, min_amounts: list[int]) -> str:
        return await self._send(
            self.pool.address,
            abi.encode_remove_liquidity(
                lp_amount, min_amounts, dynamic=self.pool.dynamic_arrays
            ),
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

    async def deposit_and_stake(
        self, amounts: list[int], min_mint: int, *, underlying: bool = False
    ) -> str:
        """Deposit and stake in one transaction, through Curve's zap.

        The arguments mirror what the panel already knows: which route is
        live decides where the coins go, which coin list is sent, and which
        array shape the inner deposit uses.

        `use_underlying` is always false here, and that is not a shortcut.
        Curve's own client sets it only for lending pools and old crypto
        metapools that have *no* deposit zap of their own -- and this app's
        underlying route is defined by having one, so the flag can never
        apply. Lending pools' underlying route is not offered here at all.

        The pool argument is separate from the deposit address for the same
        reason it is in `curve.zaps`: a factory zap serves every metapool on
        its base pool and has to be told which, while a per-pool zap was
        deployed for one and takes none.
        """
        zap = stake_zap_for(self.pool)
        if zap is None:
            raise PoolCallFailed(
                "No deposit-and-stake zap is deployed for this network, so "
                "depositing and staking are two transactions here."
            )
        if underlying:
            deposit_zap = self._zap()
            deposit_to = deposit_zap.address
            coins = [coin.address for coin in self.pool.display_coins]
            dynamic = deposit_zap.dynamic
            for_pool = self.pool.address if deposit_zap.pool_arg else ZERO_ADDRESS
        else:
            deposit_to = self.pool.address
            coins = [coin.address for coin in self.pool.pool_coins]
            dynamic = self.pool.dynamic_arrays
            for_pool = ZERO_ADDRESS
        return await self._send(
            zap.address,
            abi.encode_deposit_and_stake(
                deposit_to,
                self.pool.lp_token,
                self.pool.gauge,
                coins,
                amounts,
                min_mint,
                use_dynarray=dynamic,
                pool=for_pool,
                use_underlying=False if zap.use_underlying_arg else None,
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
