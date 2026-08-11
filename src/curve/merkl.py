"""Campaigns paid through Merkl, which Curve's own API only half reports.

A Merkl campaign streams a reward to whoever holds a particular token,
and for a Curve pool that token is either the **LP token itself** or the
**gauge** you staked it into. Merkl usually runs both, because a campaign
that only paid the gauge would pay nothing to the liquidity sitting
outside it -- so one pool is commonly two opportunities at two slightly
different rates.

**Curve's `merkle_apr` is only one of them.** Measured on the frxUSD/USP
pool: v2 reported `325.0632316262278`, which is to the digit the APR of
the *gauge* opportunity; the LP one beside it paid `325.1121240372897`
and appears in no Curve field at all. So the number this app used to
print as "merkle" was the staked half of a two-sided campaign, with
nothing saying which half -- and nothing naming the token being paid,
which is what somebody deciding whether to deposit actually wants.

**Points are the other half of what was missing.** Merkl distributes them
through the same machinery as tokens and marks them `type: "POINT"`: no
price, and therefore `apr: 0` and `dailyRewards: 0`. A points campaign is
consequently invisible in every APR field there is, on both APIs, while
being the entire reason some pools have liquidity. They are shown as what
they are -- a named reward with no rate -- rather than as a nought.

`PRETGE` is a third type, a token that exists before its generation
event. Merkl prices those and quotes an APR for them, so they are treated
as tokens; `PIKU` above is one.

**And the token named is not always the token paid.** A campaign can be
denominated in a *wrapper* whose `onClaim` hook delivers something else --
see `MerklToken`. `underlyingTokenId` on the reward token is how that is
found, and `/v4/tokens?id=…&id=…` resolves every wrapper on a chain in one
request, because unlike `identifier` on `/v4/opportunities` that parameter
repeats.

  GET /v4/opportunities?chainId=&mainProtocolId=curve&status=LIVE&items=100

`items` is capped at 100 and there were 44 live Curve opportunities
across every chain when this was written, so one request per chain is the
whole picture with room to spare -- and `/v4/opportunities/count` says how
many there are without fetching them, which is what the paging loop below
would otherwise have to guess at.

**CORS**: Merkl echoes the request's `Origin` rather than sending `*`, and
allows GET, so the browser build reads it directly. Verified against the
live host; see `curve.http` for why no header may be added to that request.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

MERKL_API = "https://api.merkl.xyz/v4"

#: Where a human goes to see a campaign, and to claim what it owes them.
#: The address-based path 301s to this one, so the opportunity id is what
#: gets linked -- it is in the payload already and costs no redirect.
MERKL_APP = "https://app.merkl.xyz"

#: Merkl's cap on `items`; anything larger is a 400 with a validation body.
MAX_ITEMS = 100

#: Reward token types Merkl prices, and therefore quotes an APR for.
#: `PRETGE` is a pre-TGE token -- not yet tradeable, but Merkl carries a
#: price for it, so its campaign has a rate like any other.
PRICED_TYPES = frozenset({"TOKEN", "PRETGE"})

#: What Merkl calls a reward with no price. It still has an address and a
#: symbol; what it does not have is a rate, and inventing one would be the
#: only way to fit it into an APR column.
POINT_TYPE = "POINT"

#: Percentage points within which the two sides of a campaign count as
#: paying the same rate. They are funded as one campaign and drift apart
#: only by however much of the liquidity happens to be staked right now --
#: 325.1121% against 325.0632% on the frxUSD/USP pool, which is a
#: distinction worth no second line.
SAME_RATE = 0.5


@dataclass(frozen=True, slots=True)
class MerklToken:
    """One thing a campaign pays out.

    Not always the thing that arrives. A **Merkl wrapper** is an ERC-20
    with an `onClaim` hook: the campaign is denominated in the wrapper,
    the hook runs when somebody claims, and what lands in the wallet is
    the *underlying* -- pulled from the incentiviser's address, withdrawn
    from Aave, unwrapped from wETH, or deposited into a vault, depending
    on which of Merkl's four templates was used. Their own documentation
    puts it plainly: the wrapper is invisible to the claimer.

    So the pyUSD/crvUSD pool advertises `ybwcrvUSD`, which is
    "Yield Basis crvUSD (Merkl wrapper)", and pays **crvUSD**. Three of
    the fourteen tokens paying live Curve campaigns were wrappers when
    this was written -- also `mtwCARROT` for CARROT and `veMEZO` for
    MEZO -- so it is not a curiosity. Merkl's own UI shows the wrapper
    symbol (`displaySymbol` is the wrapper's); this app shows what
    arrives and names the wrapper beside it.
    """

    symbol: str
    address: str
    #: True when Merkl types this `POINT`: no price, so no APR, ever.
    points: bool = False
    #: Merkl's id for the underlying, when this is a wrapper. Resolving it
    #: costs a request, so parsing records the id and `with_underlying`
    #: fills in the token once somebody has fetched it.
    underlying_id: str = ""
    #: The token a claimer actually receives. None when this is not a
    #: wrapper, and also when the lookup did not come back -- in which
    #: case the wrapper's own symbol is shown, which is what Merkl shows.
    underlying: MerklToken | None = None

    @property
    def wrapped(self) -> bool:
        return self.underlying is not None

    @property
    def paid_symbol(self) -> str:
        """What to call this on screen: what arrives, not what is counted."""
        return self.underlying.symbol if self.underlying else self.symbol

    @property
    def paid_address(self) -> str:
        """And whose logo to draw. curve-assets has crvUSD, not ybwcrvUSD."""
        return self.underlying.address if self.underlying else self.address


@dataclass(frozen=True, slots=True)
class MerklCampaign:
    """One live Merkl opportunity, as it bears on a Curve pool.

    `identifier` is the token the campaign watches -- the pool's LP token
    for a campaign paying unstaked liquidity, the gauge for one paying
    staked liquidity. Which of the two it is cannot be read off the
    campaign; it is decided by `split`, which knows the pool's addresses.
    """

    chain_id: int
    identifier: str
    name: str
    apr: float
    opportunity_id: str
    tokens: tuple[MerklToken, ...] = ()

    @property
    def url(self) -> str:
        """The campaign's page on Merkl, where it can also be claimed."""
        if not self.opportunity_id:
            return f"{MERKL_APP}/protocols/curve"
        return f"{MERKL_APP}/opportunities/{self.opportunity_id}"

    @property
    def paid_tokens(self) -> tuple[MerklToken, ...]:
        return tuple(token for token in self.tokens if not token.points)

    @property
    def point_tokens(self) -> tuple[MerklToken, ...]:
        return tuple(token for token in self.tokens if token.points)

    @property
    def points_only(self) -> bool:
        """Nothing here has a price, so nothing here has a rate."""
        return bool(self.tokens) and not self.paid_tokens


@dataclass(frozen=True, slots=True)
class MerklRewards:
    """Everything Merkl pays one pool, split by where the liquidity sits.

    Both sides usually exist and usually pay the same token at nearly the
    same rate. They are kept apart rather than merged because the choice
    between them is a real one: on a pool whose gauge campaign has ended
    and whose LP campaign has not, staking costs you the reward.
    """

    #: Campaigns on the pool or its LP token: paid whether or not you stake.
    unstaked: tuple[MerklCampaign, ...] = ()
    #: Campaigns on the gauge: paid only on what is staked into it.
    staked: tuple[MerklCampaign, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.unstaked or self.staked)

    @property
    def all(self) -> tuple[MerklCampaign, ...]:
        return self.unstaked + self.staked

    @property
    def apr(self) -> float:
        """The best rate on offer, which is the one a total should carry.

        Summed within a side and maxed across the two: a pool paying two
        tokens to unstaked liquidity earns both, while the staked campaign
        beside it is an alternative rather than an addition. Adding all
        four would report a yield nobody can reach.
        """
        return max(
            (sum(c.apr for c in side) for side in (self.unstaked, self.staked)),
            default=0.0,
        )

    @property
    def tokens(self) -> tuple[MerklToken, ...]:
        """Every distinct token paid, priced ones first, in payout order."""
        return _distinct(c.tokens for c in self.all)

    @property
    def points(self) -> tuple[MerklToken, ...]:
        return tuple(token for token in self.tokens if token.points)

    def sides_for(self, token: MerklToken) -> tuple[tuple[str, float], ...]:
        """How to say what this token pays: `(qualifier, apr)` per row.

        One row where both sides pay the same, which is the usual case and
        where a breakdown would be two lines saying 325.11% and 325.06%.
        Two rows where they genuinely differ. Nothing is hidden by the
        collapse: each campaign is listed with its own exact rate in the
        pool page's campaigns block either way.

        The single-sided cases get the qualifier that matters most,
        because that is where somebody loses money by guessing: a campaign
        paying only unstaked liquidity is one that **staking turns off**,
        and a campaign paying only the gauge is one you get nothing from
        until you stake.
        """
        unstaked, staked = self.rate_for(token)
        if unstaked and staked:
            if abs(unstaked - staked) <= SAME_RATE:
                return (("", max(unstaked, staked)),)
            return (("unstaked LP", unstaked), ("staked", staked))
        if unstaked:
            return (("unstaked LP only", unstaked),)
        if staked:
            return (("staked only", staked),)
        return ()

    def rate_for(self, token: MerklToken) -> tuple[float, float]:
        """`(unstaked, staked)` APR for one token, zero where it is not paid.

        A campaign quotes one rate for all of its tokens together, so a
        token's rate is its campaign's -- which is exact for the usual
        one-token campaign and the only honest reading for the rest.
        """
        return (
            _rate(self.unstaked, token),
            _rate(self.staked, token),
        )


def _rate(campaigns: Sequence[MerklCampaign], token: MerklToken) -> float:
    return sum(c.apr for c in campaigns if token in c.tokens)


def _distinct(groups: Iterable[Sequence[MerklToken]]) -> tuple[MerklToken, ...]:
    """Flatten token lists, dropping repeats, priced tokens first."""
    seen: list[MerklToken] = []
    for group in groups:
        for token in group:
            if token not in seen:
                seen.append(token)
    return tuple(sorted(seen, key=lambda t: t.points))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_opportunities(payload: Any) -> list[MerklCampaign]:
    """Read `/v4/opportunities` into campaigns, skipping anything not live.

    The reward tokens are in `rewardsRecord.breakdowns[].token` and *not*
    in the top-level `tokens[]`, which lists what the watched contract
    holds -- for a Curve pool that is its own coins, so taking that list
    would report USDC as the reward.
    """
    if not isinstance(payload, list):
        return []
    campaigns = []
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("status") != "LIVE":
            continue
        identifier = raw.get("identifier") or ""
        if not identifier:
            continue
        campaigns.append(
            MerklCampaign(
                chain_id=int(raw.get("chainId") or 0),
                identifier=identifier,
                name=raw.get("name") or "",
                apr=_float(raw.get("apr")),
                opportunity_id=str(raw.get("id") or ""),
                tokens=_tokens(raw),
            )
        )
    return campaigns


def parse_token(raw: Any) -> MerklToken | None:
    """One entry of `/v4/tokens`, or of a reward breakdown."""
    if not isinstance(raw, dict) or not (raw.get("symbol") or raw.get("name")):
        return None
    kind = (raw.get("type") or "").upper()
    return MerklToken(
        symbol=raw.get("symbol") or raw.get("name") or "?",
        address=raw.get("address") or "",
        # Anything Merkl does not price is treated as points. The two
        # named types are the ones seen in the wild; a new unpriced type
        # showing up as a nameless APR would be worse than showing it as
        # a reward with no rate, which it would be.
        points=kind == POINT_TYPE or kind not in PRICED_TYPES,
        underlying_id=_underlying_id(raw),
    )


def _underlying_id(raw: dict[str, Any]) -> str:
    """The wrapper's underlying, ignoring the ones that point at themselves.

    `underlyingTokenId` is on more tokens than are wrappers: `WFRAX` and
    `tGBP` both carry it set to their **own** id, which says "there is
    nothing behind this" in the same field that elsewhere says what a
    claim really pays. Following that one would print "WFRAX pays WFRAX",
    and, worse, would make the two cases indistinguishable in code.
    """
    underlying = str(raw.get("underlyingTokenId") or "")
    return "" if underlying == str(raw.get("id") or "") else underlying


def parse_tokens(payload: Any) -> dict[str, MerklToken]:
    """`/v4/tokens?id=…&id=…`, keyed by Merkl's id. The `id` param repeats
    on this endpoint, unlike `identifier` on `/v4/opportunities`, so every
    wrapper on a chain resolves in one request."""
    if not isinstance(payload, list):
        return {}
    found = {}
    for raw in payload:
        token = parse_token(raw)
        if token is not None and isinstance(raw, dict) and raw.get("id"):
            found[str(raw["id"])] = token
    return found


def underlying_ids(campaigns: Iterable[MerklCampaign]) -> set[str]:
    """Every wrapper's underlying that still needs looking up."""
    return {
        token.underlying_id
        for campaign in campaigns
        for token in campaign.tokens
        if token.underlying_id
    }


def with_underlying(
    campaigns: Iterable[MerklCampaign], resolved: dict[str, MerklToken]
) -> list[MerklCampaign]:
    """Attach what each wrapper actually pays. Unresolved ones are left alone."""

    def fill(token: MerklToken) -> MerklToken:
        underlying = resolved.get(token.underlying_id)
        if underlying is None:
            return token
        return replace(token, underlying=underlying)

    return [
        replace(campaign, tokens=tuple(fill(t) for t in campaign.tokens))
        for campaign in campaigns
    ]


def _tokens(raw: dict[str, Any]) -> tuple[MerklToken, ...]:
    record = raw.get("rewardsRecord")
    breakdowns = record.get("breakdowns") if isinstance(record, dict) else None
    tokens: list[MerklToken] = []
    for entry in breakdowns or []:
        found = parse_token(entry.get("token") if isinstance(entry, dict) else None)
        if found is not None and found not in tokens:
            tokens.append(found)
    return tuple(tokens)


def by_identifier(campaigns: Iterable[MerklCampaign]) -> dict[str, list[MerklCampaign]]:
    """Group campaigns by the (lowercased) address they watch."""
    index: dict[str, list[MerklCampaign]] = {}
    for campaign in campaigns:
        index.setdefault(campaign.identifier.lower(), []).append(campaign)
    return index


def split(
    index: dict[str, list[MerklCampaign]],
    *,
    pool: str = "",
    lp_token: str = "",
    gauge: str = "",
) -> MerklRewards:
    """Find this pool's campaigns and say which side of the gauge each is on.

    Three addresses because a campaign watches a *token*, and which token
    that is depends on the pool's age: a factory pool is its own LP token
    and is watched at the pool address, while an old-registry pool's LP
    token is a separate contract. Both are asked for; the gauge decides
    the other side.
    """
    seen: set[int] = set()

    def look(*addresses: str) -> tuple[MerklCampaign, ...]:
        found: list[MerklCampaign] = []
        for address in addresses:
            for campaign in index.get((address or "").lower(), ()):
                # A pool that *is* its own LP token -- every factory pool
                # -- would otherwise be counted twice, and the APR with
                # it. By identity rather than by opportunity id, which a
                # malformed payload can leave empty on more than one.
                if id(campaign) in seen:
                    continue
                seen.add(id(campaign))
                found.append(campaign)
        return tuple(found)

    # The gauge first: a pool whose LP token address is somehow also its
    # gauge should read as staked, which is the stricter claim.
    staked = look(gauge)
    return MerklRewards(unstaked=look(pool, lp_token), staked=staked)


#: An empty result, for a chain Merkl has never heard of and for every
#: pool before the lookup has landed.
NO_REWARDS = MerklRewards()
