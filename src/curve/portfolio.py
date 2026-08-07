"""What an address is actually holding, across every pool on a chain.

The question "which pools am I in?" has no endpoint behind it. Curve's
API knows what pools exist; only the chain knows what you hold. So this
asks the chain about all of them: one `balanceOf` per LP token and one
per gauge, batched through Multicall3.

**It is cheaper than it sounds.** Measured against a public endpoint on
Ethereum, where 1,060 pools have any TVL at all and 656 of them have a
live gauge -- 1,713 calls:

    batch   in flight   scan
    100     1           1.41s
    500     1           0.72s
    300     6           0.36s
    1713    1           0.95s   (one call: ~380KB of calldata, accepted)

So the scan is not the slow part; discovering the pools is. Both public
endpoints tried took the whole thing in a single call, which means the
batching here is insurance against a stricter RPC rather than a
necessity -- hence `CHUNK` well under what worked, and a handful in
flight.

**Valuing a position** needs a price for the LP token, and there is no
endpoint for that either. The arithmetic is `tvl_usd / total_supply`:
the TVL comes from the API, and the supply is read from the chain in a
second, much smaller batch -- only the pools that turned out to hold
something, which is usually under twenty calls. Reading it rather than
taking the API's copy also keeps the two numbers from different blocks
apart by less, and it is the one shape that works on the Lite chains
too, where the listing carries no supply.

That is the pool's own accounting rather than a market price, which is
the right one here: it is what the pool would pay out.

Nothing in this module imports Flet, and nothing in it knows about
caching or progress bars beyond calling back.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from wallet.base import WalletProvider
from wallet.erc20 import encode_balance_of

from . import abi
from .multicall import MULTICALL3, decode_uints, encode_aggregate3

#: Calls per batch. Both endpoints measured took all 1,713 at once, so
#: this is deliberately conservative: a chunk this size is ~67KB of
#: calldata, which no reasonable RPC refuses.
CHUNK = 300

#: Batches in flight. Six was the fastest measured and is polite enough
#: for a public endpoint that rate-limits by request.
CONCURRENCY = 6

#: Wei-per-unit for an LP token. Every Curve LP token is 18 decimals --
#: it is minted by the pool, not by whoever made the coins.
LP_UNIT = 10**18


@dataclass(frozen=True, slots=True)
class Holding:
    """One pool, and what this address has in it."""

    address: str
    name: str
    chain: str
    #: Raw balances, in LP wei.
    wallet: int = 0
    staked: int = 0
    #: The pool's own numbers, for pricing the LP token. `supply` is in
    #: whole tokens, as the API reports TVL in whole dollars.
    tvl: float = 0.0
    supply: float = 0.0
    #: `(address, symbol)` per coin. The address is what the token marks
    #: are drawn from -- curve-assets names its images by address -- so a
    #: symbol on its own would give lettered discs where the pool list
    #: shows real logos.
    coins: tuple[tuple[str, str], ...] = ()
    #: Kept so a remembered holding can be re-read on its own, before any
    #: pool list has loaded. That is the whole point of remembering it.
    lp_token: str = ""
    gauge: str = ""

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(symbol for _address, symbol in self.coins)

    @property
    def total(self) -> int:
        return self.wallet + self.staked

    @property
    def lp_price(self) -> float:
        """USD per LP token, by the pool's own accounting.

        Zero when the pool reports no supply, which is what an empty pool
        reports -- and dividing by it would be an exception rather than a
        price.
        """
        return self.tvl / self.supply if self.supply else 0.0

    @property
    def value(self) -> float:
        """What the position is worth, in USD."""
        return self.total / LP_UNIT * self.lp_price

    @property
    def share(self) -> float:
        """The fraction of the pool this is, 0-1."""
        return (self.total / LP_UNIT) / self.supply if self.supply else 0.0


@dataclass(frozen=True, slots=True)
class Target:
    """A pool, and the two addresses worth asking about it."""

    address: str
    name: str
    chain: str
    lp_token: str
    gauge: str = ""
    tvl: float = 0.0
    supply: float = 0.0
    coins: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def calls_for(targets: Sequence[Target]) -> list[str]:
    """The contracts to ask, in order: LP token, then gauge where there
    is one.

    A pool with no gauge contributes one call and a pool with one
    contributes two, so position in the answer list is not position in
    the target list -- `holdings_from` walks them back together.
    """
    plan: list[str] = []
    for target in targets:
        plan.append(target.lp_token or target.address)
        if target.gauge:
            plan.append(target.gauge)
    return plan


def holdings_from(
    targets: Sequence[Target], answers: Sequence[int | None]
) -> list[Holding]:
    """Fold the flat answer list back into one row per pool.

    Only pools with something in them come back, sorted by what the
    position is worth -- which is the order the question was asked in.
    """
    wallet: dict[int, int] = {}
    staked: dict[int, int] = {}
    position = 0
    for index, target in enumerate(targets):
        wallet[index] = answers[position] or 0 if position < len(answers) else 0
        position += 1
        if target.gauge:
            staked[index] = answers[position] or 0 if position < len(answers) else 0
            position += 1

    found = [
        Holding(
            address=target.address,
            name=target.name,
            chain=target.chain,
            wallet=wallet.get(index, 0),
            staked=staked.get(index, 0),
            tvl=target.tvl,
            supply=target.supply,
            coins=target.coins,
            lp_token=target.lp_token,
            gauge=target.gauge,
        )
        for index, target in enumerate(targets)
        if wallet.get(index, 0) or staked.get(index, 0)
    ]
    found.sort(key=lambda holding: (holding.value, holding.total), reverse=True)
    return found


async def scan(
    provider: WalletProvider,
    targets: Sequence[Target],
    account: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    chunk: int = CHUNK,
    concurrency: int = CONCURRENCY,
) -> list[Holding]:
    """Read every balance and return what is not zero.

    `on_progress` is called with `(done, total)` in calls, after each
    batch -- a full scan is a second or so, and a second with no feedback
    reads as a broken page.
    """
    plan = calls_for(targets)
    if not plan:
        return []
    balances = await _batched(
        provider, plan, encode_balance_of(account), chunk, concurrency, on_progress
    )
    holdings = holdings_from(targets, balances)
    return await with_supply(provider, holdings)


async def with_supply(
    provider: WalletProvider, holdings: Sequence[Holding]
) -> list[Holding]:
    """Fill in each LP token's supply, which is what prices the position.

    A second batch, and a small one: however many pools the scan turned
    up, which is a handful. Anything that does not answer keeps whatever
    supply it already had, so a remembered value survives a chain that
    will not talk.
    """
    if not holdings:
        return list(holdings)
    supplies = await _batched(
        provider,
        [holding.lp_token or holding.address for holding in holdings],
        abi.encode_total_supply(),
        CHUNK,
        1,
        None,
    )
    priced = [
        dataclasses.replace(holding, supply=supply / LP_UNIT) if supply else holding
        for holding, supply in zip(holdings, supplies, strict=False)
    ]
    priced.sort(key=lambda holding: (holding.value, holding.total), reverse=True)
    return priced


async def _batched(
    provider: WalletProvider,
    contracts: Sequence[str],
    data: str,
    chunk: int,
    concurrency: int,
    on_progress: Callable[[int, int], None] | None,
) -> list[int | None]:
    """The same call against many contracts, in batches, concurrently."""
    batches = [contracts[i : i + chunk] for i in range(0, len(contracts), chunk)]
    answers: list[list[int | None]] = [[] for _ in batches]
    done = 0
    gate = asyncio.Semaphore(max(1, concurrency))

    async def run(number: int, batch: Sequence[str]) -> None:
        nonlocal done
        async with gate:
            result = await provider.call(
                MULTICALL3, encode_aggregate3([(target, data) for target in batch])
            )
        answers[number] = decode_uints(result, len(batch))
        done += len(batch)
        if on_progress is not None:
            on_progress(done, len(contracts))

    await asyncio.gather(*[run(n, batch) for n, batch in enumerate(batches)])
    return [value for group in answers for value in group]


# -- what to remember between visits ---------------------------------------
#
# A scan is fast but not instant, and the answer barely changes between
# one visit and the next. So the last one is kept, shown immediately, and
# then corrected -- first for the pools already known (a handful of calls,
# so the numbers on screen are right almost at once), then for everything
# else.


def to_json(holdings: Sequence[Holding], account: str, chain: str) -> dict[str, Any]:
    """The shape that goes into storage. Small: positions, not pools."""
    return {
        "account": account.lower(),
        "chain": chain,
        "holdings": [
            {
                "address": holding.address,
                "name": holding.name,
                "wallet": str(holding.wallet),
                "staked": str(holding.staked),
                "tvl": holding.tvl,
                "supply": holding.supply,
                "coins": [list(coin) for coin in holding.coins],
                "lp_token": holding.lp_token,
                "gauge": holding.gauge,
            }
            for holding in holdings
        ],
    }


def from_json(payload: Any, account: str, chain: str) -> list[Holding]:
    """Read it back, for this account and chain only.

    Anything else -- another account, another chain, a shape from an
    older version -- is not an error worth reporting, it is simply
    nothing remembered.
    """
    if not isinstance(payload, dict):
        return []
    if payload.get("account") != account.lower() or payload.get("chain") != chain:
        return []
    holdings = []
    for raw in payload.get("holdings") or []:
        try:
            holdings.append(
                Holding(
                    address=str(raw["address"]),
                    name=str(raw.get("name") or ""),
                    chain=chain,
                    # Balances are stored as strings: they are up to 256
                    # bits and JSON numbers are doubles.
                    wallet=int(raw.get("wallet") or 0),
                    staked=int(raw.get("staked") or 0),
                    tvl=float(raw.get("tvl") or 0.0),
                    supply=float(raw.get("supply") or 0.0),
                    coins=tuple(
                        (str(coin[0]), str(coin[1]))
                        for coin in raw.get("coins") or ()
                        if isinstance(coin, (list, tuple)) and len(coin) == 2
                    ),
                    lp_token=str(raw.get("lp_token") or ""),
                    gauge=str(raw.get("gauge") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return holdings


def targets_for(holdings: Sequence[Holding]) -> list[Target]:
    """Targets for a quick refresh of rows that are already on screen.

    Built from the holdings themselves rather than from a pool list,
    which is the point: a remembered position can be re-read before the
    API has said anything at all, so the numbers are right within a
    request or two of opening the page.
    """
    return [
        Target(
            address=holding.address,
            name=holding.name,
            chain=holding.chain,
            lp_token=holding.lp_token or holding.address,
            gauge=holding.gauge,
            tvl=holding.tvl,
            coins=holding.coins,
        )
        for holding in holdings
    ]
