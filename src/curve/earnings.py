"""What a position is earning, and what it has earned but not taken.

The portfolio page answers "which pools am I in, and for how much". This
answers the two questions that follow: at what rate, and how much is
sitting in the gauge waiting.

**An APR is not one number.** Curve pays CRV on a *working balance*, not
on what you staked:

    working = min(0.4 * b + 0.6 * S * veShare, b)

so the same pool pays a veCRV holder up to two and a half times what it
pays somebody with none. The published `crv_apr` is the unboosted rate and
`crv_apr_boosted` the ceiling; neither is what any particular account
earns. The boost is the only per-account input -- everything else in the
rate belongs to the pool -- and it is one read per gauge.

Two further things the number has to be honest about:

  * **CRV and incentives are paid on the staked part only.** LP sitting in
    the wallet earns the pool's trading fees and nothing else, so a
    position half unstaked earns half the headline rate. That is a
    property of the position, not of the pool, so it belongs here.
  * **An unstaked position has no boost at all**, because it has no
    working balance. Showing the pool's boosted ceiling next to it would
    be advertising a rate that address cannot get.

**Reads batch, and so does half the writing.** Everything read here goes
through Multicall3 in a handful of round trips, `claimable_tokens`
included -- it is declared `nonpayable` because it checkpoints, but inside
an `eth_call` the checkpoint is simulated and discarded. Claiming batches
too, but by two different routes and only within each: see `ClaimPlan`.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from wallet.base import WalletProvider

from . import abi
from .multicall import MULTICALL3, decode_uints, encode_aggregate3
from .rewards import CRV_DECIMALS, REWARDS

#: The share of a balance that earns CRV with no veCRV at all. Curve's
#: `working_balance` floor, and the denominator of the boost.
UNBOOSTED_SHARE = 0.4

#: The most a boost can be, which is `1 / UNBOOSTED_SHARE`. Read values
#: are clamped to it: a gauge mid-checkpoint can briefly report a working
#: balance above the deposit, and 2.5x is the protocol's own ceiling.
MAX_BOOST = 1.0 / UNBOOSTED_SHARE

#: Calls per Multicall3 batch. Well under what `curve.portfolio` measured
#: as acceptable, because a portfolio is a handful of pools rather than a
#: thousand.
CHUNK = 200

#: How many `reward_tokens(i)` to walk per gauge. Gauges cap this at eight
#: themselves -- the same bound `curve.pool` uses.
MAX_REWARD_TOKENS = 8


@dataclass(frozen=True, slots=True)
class Reward:
    """One reward token, and what is owed in it."""

    token: str
    symbol: str
    decimals: int
    amount: int = 0
    #: USD per whole token, or 0 where nothing published one.
    price: float = 0.0

    @property
    def whole(self) -> float:
        return self.amount / 10**self.decimals

    @property
    def value(self) -> float:
        return self.whole * self.price


@dataclass(frozen=True, slots=True)
class Earning:
    """What one pool is paying this account, and what it owes it."""

    pool: str
    gauge: str = ""
    #: Raw LP, as `Holding` carries it.
    staked: int = 0
    wallet: int = 0
    #: The gauge's own boosted balance for this account.
    working: int = 0
    #: The pool's published rates: unboosted CRV, boosted ceiling, and the
    #: sum of every incentive token's APR.
    crv_apr: float = 0.0
    crv_apr_max: float = 0.0
    incentive_apr: float = 0.0
    rewards: tuple[Reward, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return self.staked + self.wallet

    @property
    def boost(self) -> float:
        """1.0 to 2.5, or 0.0 when nothing is staked.

        Zero rather than one for an unstaked position, because "no boost"
        and "the minimum boost" are different states: the first earns no
        CRV at all, and printing 1.00x next to it suggests otherwise.
        """
        if self.staked <= 0 or self.working <= 0:
            return 0.0
        return min(self.working / (self.staked * UNBOOSTED_SHARE), MAX_BOOST)

    @property
    def staked_share(self) -> float:
        """How much of the position is actually earning rewards, 0-1."""
        return self.staked / self.total if self.total else 0.0

    @property
    def user_crv_apr(self) -> float:
        """The CRV rate this account gets, at its boost, on its whole position.

        The published rate applies to staked LP, so it is scaled by how
        much of the position is staked -- otherwise a wallet holding one
        LP token staked and nine loose would be shown the full rate.
        """
        return self.crv_apr * self.boost * self.staked_share

    @property
    def user_incentive_apr(self) -> float:
        """Incentives are not boosted, but they are still staked-only."""
        return self.incentive_apr * self.staked_share

    @property
    def user_apr(self) -> float:
        """Everything the gauge pays this account, as one rate."""
        return self.user_crv_apr + self.user_incentive_apr

    @property
    def claimable_value(self) -> float:
        return sum(reward.value for reward in self.rewards)

    @property
    def has_crv(self) -> bool:
        return any(
            reward.symbol == "CRV" and reward.amount > 0 for reward in self.rewards
        )

    @property
    def has_extras(self) -> bool:
        return any(
            reward.symbol != "CRV" and reward.amount > 0 for reward in self.rewards
        )


@dataclass(frozen=True, slots=True)
class ClaimPlan:
    """The transactions "claim everything" actually comes to.

    Two lists rather than one, because CRV and the incentive tokens are
    claimed from different contracts and they batch by different means.

    **CRV** is minted, and the Minter mints for `msg.sender` alone, so
    these have to come from the user's own address. `mint_many` takes a
    fixed-size array of gauges -- eight on Ethereum's Minter, thirty-two
    on the sidechain factories -- so a portfolio is one transaction per
    batch of that size, and `crv` is already chunked to it.

    **Incentives** are transfers, not mints, and `claim_rewards(address)`
    pays the address it is given rather than its caller: the gauge's
    `_checkpoint_rewards` resolves the receiver from `_user`, and the only
    thing reserved to `msg.sender` is redirecting the payment somewhere
    else. So a stranger can claim on an owner's behalf, the tokens land
    with the owner, and every gauge in a portfolio goes into one
    Multicall3 transaction.

    That contradicts what this docstring used to say, and the correction
    is worth keeping: the original test batched through `aggregate3` with
    `allowFailure=true`, which turns an inner revert into a mined
    transaction that moved nothing. It was read as a refusal. It was not
    one -- the call had succeeded, and a second bad measurement (anvil
    frees a snapshot when you revert to it, so the follow-up ran against
    already-claimed state) agreed with the first. Claims are sent with
    `allowFailure=false` now, so a gauge that will not pay reverts the
    transaction instead of being quietly dropped.
    """

    #: `(minter, [gauge, ...])` per transaction, already chunked.
    crv: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)
    #: Every gauge owing an incentive token, claimed in one batch.
    extras: tuple[str, ...] = field(default_factory=tuple)

    @property
    def transactions(self) -> int:
        return len(self.crv) + (1 if self.extras else 0)


def claim_plan(chain_id: int, earnings: list[Earning]) -> ClaimPlan:
    """What to send to collect everything owed across a portfolio."""
    entry = REWARDS.get(chain_id)
    crv_gauges = [e.gauge for e in earnings if e.gauge and e.has_crv]
    batches: list[tuple[str, tuple[str, ...]]] = []
    if entry is not None and crv_gauges:
        size = entry.mint_many_size
        batches = [
            (entry.minter, tuple(crv_gauges[start : start + size]))
            for start in range(0, len(crv_gauges), size)
        ]
    return ClaimPlan(
        crv=tuple(batches),
        extras=tuple(e.gauge for e in earnings if e.gauge and e.has_extras),
    )




# -- reading ---------------------------------------------------------------


async def _batch(
    provider: WalletProvider, calls: list[tuple[str, str]]
) -> list[int | None]:
    """One Multicall3 round. `None` per call that failed.

    A gauge that does not implement `reward_count` is an ordinary answer
    rather than a reason to abandon the rest, which is the same rule
    `curve.pool._read_many` follows -- and a chain with no Multicall3 at
    all comes back as every call failing, which degrades to "no rewards
    shown" rather than to an error.
    """
    answers: list[int | None] = []
    for start in range(0, len(calls), CHUNK):
        chunk = calls[start : start + CHUNK]
        try:
            raw = await provider.call(MULTICALL3, encode_aggregate3(chunk))
            answers += decode_uints(raw, len(chunk))
        except Exception:
            answers += [None] * len(chunk)
    return answers


def _word_to_address(value: int) -> str:
    return "0x" + f"{value:040x}"


async def read_earnings(
    provider: WalletProvider,
    account: str,
    positions: list[Earning],
    *,
    crv_price: float = 0.0,
    token_meta: dict[str, tuple[str, int, float]] | None = None,
) -> list[Earning]:
    """Fill in each position's boost and what it is owed.

    `token_meta` maps a reward token's address to `(symbol, decimals,
    price)`. It comes from the pool payload, which publishes all three per
    incentive token -- reading them from the chain instead would be two
    more calls per token for facts the API already handed over, and the
    decimals matter: a 6-decimal reward rendered as 18 is wrong by a
    factor of a trillion.

    Three rounds, because each depends on the last: the per-gauge numbers
    and how many incentive tokens there are; which tokens those are; then
    what is owed in each. A portfolio is a handful of pools, so that is
    three round trips in total rather than three per pool.
    """
    meta = {key.lower(): value for key, value in (token_meta or {}).items()}
    staked = [p for p in positions if p.gauge and p.staked > 0]
    if not staked or not account:
        return positions
    earning_pools = {p.pool for p in staked}

    first: list[tuple[str, str]] = []
    for position in staked:
        first += [
            (position.gauge, abi.encode_working_balances(account)),
            (position.gauge, abi.encode_claimable_tokens(account)),
            (position.gauge, abi.encode_reward_count()),
        ]
    answers = await _batch(provider, first)

    working: dict[str, int] = {}
    crv_owed: dict[str, int] = {}
    counts: dict[str, int] = {}
    for index, position in enumerate(staked):
        base = index * 3
        working[position.pool] = answers[base] or 0
        crv_owed[position.pool] = answers[base + 1] or 0
        counts[position.pool] = min(answers[base + 2] or 0, MAX_REWARD_TOKENS)

    token_calls: list[tuple[str, str]] = []
    asked_for: list[str] = []
    for position in staked:
        for slot in range(counts[position.pool]):
            token_calls.append((position.gauge, abi.encode_reward_tokens(slot)))
            asked_for.append(position.pool)
    token_answers = await _batch(provider, token_calls) if token_calls else []
    tokens: dict[str, list[str]] = {position.pool: [] for position in staked}
    for pool, answer in zip(asked_for, token_answers, strict=True):
        if answer:
            tokens[pool].append(_word_to_address(answer))

    owed_calls: list[tuple[str, str]] = []
    pairs: list[tuple[str, str]] = []
    for position in staked:
        for token in tokens[position.pool]:
            owed_calls.append(
                (position.gauge, abi.encode_claimable_reward(account, token))
            )
            pairs.append((position.pool, token))
    owed_answers = await _batch(provider, owed_calls) if owed_calls else []
    owed = {
        pair: (answer or 0)
        for pair, answer in zip(pairs, owed_answers, strict=True)
    }

    filled: list[Earning] = []
    for position in positions:
        if position.pool not in earning_pools:
            filled.append(position)
            continue
        rewards: list[Reward] = []
        crv = crv_owed.get(position.pool, 0)
        if crv > 0:
            rewards.append(Reward("", "CRV", CRV_DECIMALS, crv, crv_price))
        for token in tokens.get(position.pool, []):
            amount = owed.get((position.pool, token), 0)
            if amount <= 0:
                continue
            symbol, decimals, price = meta.get(token.lower(), ("?", 18, 0.0))
            rewards.append(Reward(token, symbol, decimals, amount, price))
        filled.append(
            dataclasses.replace(
                position,
                working=working.get(position.pool, 0),
                rewards=tuple(rewards),
            )
        )
    return filled


def seed_from_detail(earning: Earning, detail: dict) -> tuple[Earning, dict]:
    """Take the published rates off a pool payload.

    Returns the position with its rates filled in, and the `(symbol,
    decimals, price)` of every incentive token it streams -- which is
    what `read_earnings` needs to render an amount, and what saves two
    chain reads per token for facts the API already published.

    CRV is filtered out of the incentive list the same way `Pool.from_v2`
    filters it: some pools report it there as well, and counting it in
    both places would double the rate.
    """
    extras = [
        entry
        for entry in (detail.get("extra_rewards_apr") or [])
        if (entry.get("symbol") or "").upper() != "CRV"
    ]
    meta = {
        (entry.get("address") or "").lower(): (
            entry.get("symbol") or "?",
            int(entry.get("decimals") or 18),
            float(entry.get("price") or 0.0),
        )
        for entry in extras
        if entry.get("address")
    }
    return (
        dataclasses.replace(
            earning,
            crv_apr=float(detail.get("crv_apr") or 0.0),
            crv_apr_max=float(detail.get("crv_apr_boosted") or 0.0),
            incentive_apr=sum(float(entry.get("apr") or 0.0) for entry in extras),
        ),
        meta,
    )


async def send_claims(
    provider: WalletProvider, account: str, plan: ClaimPlan, *, crv: bool = True
) -> list[str]:
    """Send a plan, returning one hash per transaction, in order.

    `crv` picks which half: the page offers them as two buttons because
    they are two contracts. CRV goes one send per batch of eight (or
    thirty-two), because that is `mint_many`'s array; the incentives go in
    a single Multicall3 transaction however many gauges there are.

    Batching the incentives is safe on any chain this can be reached from:
    every number behind the buttons was read through Multicall3 by
    `read_earnings`, so a chain without that contract shows nothing owed
    and offers nothing to claim.

    Gas and nonce are left to the wallet, as everywhere else in this app.
    """
    sent: list[str] = []
    if crv:
        for minter, gauges in plan.crv:
            slots = next(
                (r.mint_many_size for r in REWARDS.values() if r.minter == minter), 32
            )
            sent.append(
                await provider.send_transaction(
                    {
                        "from": account,
                        "to": minter,
                        "value": "0x0",
                        "data": abi.encode_mint_many(list(gauges), slots),
                    }
                )
            )
        return sent
    if plan.extras:
        # `allowFailure=false`: a gauge that will not pay must take the
        # transaction down with it. See `ClaimPlan` -- allowing failures
        # here is what made a silent no-op look like a mined claim.
        calls = [
            (gauge, abi.encode_claim_rewards_for(account)) for gauge in plan.extras
        ]
        sent.append(
            await provider.send_transaction(
                {
                    "from": account,
                    "to": MULTICALL3,
                    "value": "0x0",
                    "data": encode_aggregate3(calls, allow_failure=False),
                }
            )
        )
    return sent
