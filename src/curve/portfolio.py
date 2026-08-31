"""What an address is actually holding, across every pool on a chain."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from wallet.base import WalletError, WalletProvider
from wallet.erc20 import encode_balance_of, to_checksum_address

from . import abi
from .earnings import MAX_REWARD_TOKENS
from .multicall import MULTICALL3, decode_uints, encode_aggregate3
from .rewards import REWARDS, gauge_lookup, gauges_from_batch

#: Calls per batch. Both endpoints measured took all 1,713 at once, so this
#: is deliberately conservative: a chunk this size is ~67KB of calldata,
#: which no reasonable RPC refuses.
CHUNK = 300

#: Batches in flight. Six was the fastest measured and is polite enough for
#: a public endpoint that rate-limits by request.
CONCURRENCY = 6

#: Wei-per-unit for an LP token. Every Curve LP token is 18 decimals -- it
#: is minted by the pool, not by whoever made the coins.
LP_UNIT = 10**18

#: Tries per batch before it counts as refused, and the wait before each
#: retry. A public endpoint rate-limits by request rather than erroring, and
#: it lets go again within a second or two.
ATTEMPTS = 3
BACKOFF = 0.4


@dataclass(frozen=True, slots=True)
class Holding:
    """One pool, and what this address has in it."""

    address: str
    name: str
    chain: str
    #: Raw balances, in LP wei.
    wallet: int = 0
    staked: int = 0
    #: The pool's own numbers, for pricing the LP token.
    tvl: float = 0.0
    supply: float = 0.0
    #: `(address, symbol)` per coin. The address is what the token marks
    #: are drawn from -- curve-assets names its images by address -- so
    #: a symbol on its own would give lettered discs where the pool list
    #: shows real logos.
    coins: tuple[tuple[str, str], ...] = ()
    #: Kept so a remembered holding can be re-read on its own, before
    #: any pool list has loaded.
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
        """USD per LP token, by the pool's own accounting."""
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
    """The contracts to ask, in order: LP token, then gauge where there is one."""
    plan: list[str] = []
    for target in targets:
        plan.append(target.lp_token or target.address)
        if target.gauge:
            plan.append(target.gauge)
    return plan


def holdings_from(
    targets: Sequence[Target], answers: Sequence[int | None]
) -> list[Holding]:
    """Fold the flat answer list back into one row per pool."""
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
    chain_id: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
    chunk: int = CHUNK,
    concurrency: int = CONCURRENCY,
) -> list[Holding]:
    """Read every balance and return what is not zero."""
    plan = calls_for(targets)
    if not plan:
        return []
    balances = await _batched(
        provider, plan, encode_balance_of(account), chunk, concurrency, on_progress
    )
    targets, balances = await resolve_absent_gauges(
        provider, chain_id, targets, balances, account, chunk, concurrency
    )
    holdings = holdings_from(targets, balances)
    return await with_supply(provider, holdings)


async def sweep_unclaimed(
    provider: WalletProvider,
    targets: Sequence[Target],
    account: str,
    *,
    held: Sequence[Holding] = (),
    on_progress: Callable[[int, int], None] | None = None,
    chunk: int = CHUNK,
    concurrency: int = CONCURRENCY,
) -> list[Holding]:
    """Pools whose gauge still owes this address, after it withdrew.

    Withdrawing does not claim, and `scan` keeps a pool only where the wallet
    holds LP or has some staked -- so a position emptied and left unclaimed
    goes off the portfolio taking the rewards in its gauge with it.  Nothing
    on the page then says they are there, and the only route back is
    remembering which pool it was.

    Reads per gauge, which is why this is asked for rather than part of every
    load: on Ethereum it is the same order of reads again as the whole
    balance scan.

    CRV is not enough on its own.  A gauge can pay incentive tokens and no
    CRV at all -- the pool list marks those `hasNoCrv` -- so asking only
    `claimable_tokens` would walk past exactly the position somebody is
    looking for.  The token addresses are not in the API this app reads:
    `prices.curve.finance` carries an `extra_rewards_apr` and no addresses,
    and the endpoint that does carry them is a different one, per registry,
    and only as good as what it has indexed.  So they come off the gauges,
    the way `read_earnings` already gets them: how many, which, then what
    each owes.

    Three rounds rather than one, and the last two are over the minority of
    gauges that pay anything beyond CRV.

    Pools already held are skipped: they are on the page, and what they owe
    is `read_earnings`' business.
    """
    already = {holding.address.lower() for holding in held}
    gauged = [
        target for target in targets
        if target.gauge and target.address.lower() not in already
    ]
    if not gauged or not account:
        return []

    # What CRV is owed, and how many other tokens this gauge pays at all.
    first: list[tuple[str, str]] = []
    for target in gauged:
        first += [
            (target.gauge, abi.encode_claimable_tokens(account)),
            (target.gauge, abi.encode_reward_count()),
        ]
    # Strict: this round is what decides whether anything is owed anywhere,
    # so an endpoint that would not answer it has to say so rather than come
    # back as "nothing".
    answers = await _calls(provider, first, chunk, concurrency, on_progress,
                           strict=True)
    owed: dict[str, int] = {}
    counts: dict[str, int] = {}
    for index, target in enumerate(gauged):
        owed[target.gauge] = answers[2 * index] or 0
        counts[target.gauge] = min(answers[2 * index + 1] or 0, MAX_REWARD_TOKENS)

    # Which tokens, for the gauges that pay any.
    token_calls: list[tuple[str, str]] = []
    asked: list[str] = []
    for target in gauged:
        for slot in range(counts[target.gauge]):
            token_calls.append((target.gauge, abi.encode_reward_tokens(slot)))
            asked.append(target.gauge)
    token_answers = (
        await _calls(provider, token_calls, chunk, concurrency, None)
        if token_calls else []
    )

    # And what each of those is owed.
    owed_calls: list[tuple[str, str]] = []
    for gauge, answer in zip(asked, token_answers, strict=False):
        if answer:
            owed_calls.append(
                (gauge, abi.encode_claimable_reward(account, _token_at(answer)))
            )
    extra_answers = (
        await _calls(provider, owed_calls, chunk, concurrency, None)
        if owed_calls else []
    )
    for (gauge, _data), amount in zip(owed_calls, extra_answers, strict=False):
        owed[gauge] = owed.get(gauge, 0) + (amount or 0)

    found = [
        Holding(
            address=target.address,
            name=target.name,
            chain=target.chain,
            wallet=0,
            staked=0,
            tvl=target.tvl,
            supply=target.supply,
            coins=target.coins,
            lp_token=target.lp_token,
            gauge=target.gauge,
        )
        for target in gauged
        if owed.get(target.gauge, 0) > 0
    ]
    found.sort(key=lambda holding: holding.name)
    return found


def _token_at(word: int) -> str:
    """A reward token's address out of the 32-byte word a gauge answers with."""
    return to_checksum_address("0x" + f"{word:040x}")


async def resolve_absent_gauges(
    provider: WalletProvider,
    chain_id: int,
    targets: Sequence[Target],
    balances: list[int | None],
    account: str,
    chunk: int = CHUNK,
    concurrency: int = CONCURRENCY,
) -> tuple[list[Target], list[int | None]]:
    """Read again at the gauge that is on this chain, where the listed one is not.

    The pool list names the Ethereum *root* gauge for whole chains -- every
    gauge it lists for BSC and for Sonic, 36 of 45 on Base.  A `balanceOf` on
    an address with no code is not an error: the call succeeds and returns
    nothing, which folds into a staked balance of zero, and the position
    disappears from the portfolio rather than showing up as unreadable.

    So an unread gauge is worth a second look: each of the chain's factories
    is asked what gauge it made for the LP token, and the balance is read
    again at that.  Two extra rounds, over the absent gauges only -- none at
    all on a chain whose listed gauges are all really there.
    """
    entry = REWARDS.get(chain_id)
    factories = entry.gauge_factories if entry is not None else ()
    kept = list(targets)
    answers = list(balances)
    if not factories:
        return kept, answers

    #: Where each target's gauge balance sits in the flat answer list.
    at: dict[int, int] = {}
    position = 0
    for index, target in enumerate(kept):
        position += 1
        if target.gauge:
            at[index] = position
            position += 1
    absent = [
        index
        for index, slot in at.items()
        if slot < len(answers) and answers[slot] is None
    ]
    if not absent:
        return kept, answers

    named = await _calls(provider, [
        call
        for index in absent
        for call in gauge_lookup(
            kept[index].lp_token or kept[index].address, factories)
    ], chunk, concurrency, None)
    found = {
        absent[offset]: to_checksum_address(gauge)
        for offset, (gauge, _minter) in gauges_from_batch(
            factories, len(absent), named).items()
    }
    if not found:
        return kept, answers

    order = list(found)
    staked = await _calls(
        provider,
        [(found[index], encode_balance_of(account)) for index in order],
        chunk,
        concurrency,
        None,
    )
    for index, value in zip(order, staked, strict=True):
        kept[index] = dataclasses.replace(kept[index], gauge=found[index])
        answers[at[index]] = value
    return kept, answers


async def with_supply(
    provider: WalletProvider, holdings: Sequence[Holding]
) -> list[Holding]:
    """Fill in each LP token's supply, which is what prices the position."""
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
    return await _calls(
        provider,
        [(target, data) for target in contracts],
        chunk,
        concurrency,
        on_progress,
    )


async def _calls(
    provider: WalletProvider,
    calls: Sequence[tuple[str, str]],
    chunk: int,
    concurrency: int,
    on_progress: Callable[[int, int], None] | None,
    *,
    strict: bool = False,
) -> list[int | None]:
    """Many calls, in batches, concurrently. `None` per call that said nothing.

    A batch that comes back saying nothing *at all* is retried rather than
    believed.  `decode_uints` cannot tell a truncated response from one where
    every call in it returned empty -- both are `None` the whole way down --
    and a public endpoint refuses by answering short rather than by erroring.
    Folded into the answers, that reads as "this address is owed nothing
    anywhere", which is the worst way for a read to be wrong.

    `strict` then refuses to pretend: a batch still unreadable after its
    tries raises, so the caller can say the endpoint would not answer instead
    of drawing an empty table.  Off by default, because `scan` reads gauges
    that legitimately have no code and wants the `None`s --
    `resolve_absent_gauges` is built on them.
    """
    batches = [calls[i : i + chunk] for i in range(0, len(calls), chunk)]
    answers: list[list[int | None]] = [[] for _ in batches]
    done = 0
    refused = 0
    gate = asyncio.Semaphore(max(1, concurrency))

    async def run(number: int, batch: Sequence[tuple[str, str]]) -> None:
        nonlocal done, refused
        values: list[int | None] = [None] * len(batch)
        for attempt in range(ATTEMPTS):
            try:
                async with gate:
                    result = await provider.call(
                        MULTICALL3, encode_aggregate3(list(batch))
                    )
                values = decode_uints(result, len(batch))
            except Exception:
                values = [None] * len(batch)
            if any(value is not None for value in values):
                break
            if attempt + 1 < ATTEMPTS:
                await asyncio.sleep(BACKOFF * 2**attempt)
        else:
            refused += len(batch)
        answers[number] = values
        done += len(batch)
        if on_progress is not None:
            on_progress(done, len(calls))

    await asyncio.gather(*[run(n, batch) for n, batch in enumerate(batches)])
    if strict and refused:
        raise WalletError(
            f"{refused} of {len(calls)} reads went unanswered -- the endpoint "
            f"would not take them."
        )
    return [value for group in answers for value in group]


# -- what to remember between visits ---------------------------------------
# A scan is fast but not instant, and the answer barely changes between one
# visit and the next.


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
    """Read it back, for this account and chain only."""
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
    """Targets for a quick refresh of rows that are already on screen."""
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
