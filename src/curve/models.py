"""The domain objects, parsed out of Curve's API shapes.

Everything here is a plain dataclass with no Flet and no network in sight,
which is what makes the sorting, formatting and math testable without a
running app. The API's own shapes leak into `from_api` and stop there.

Two facts about the API drive most of this file, both verified against live
responses (see docs/curve-api.md):

  * `getPools` carries no volume and no base APY -- only gauge CRV APR. The
    rest arrives from `getVolumes` and is attached afterwards, which is why
    those fields are mutable and default to zero.
  * numeric fields arrive as strings holding raw integers (`"1039823717…"`),
    as floats, or as null, sometimes for the same field on different pools.
    Every read goes through the coercion helpers below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Registries whose pools use the StableSwap ABI: `int128` coin indices.
STABLE_REGISTRIES = frozenset(
    {"main", "factory", "factory-crvusd", "factory-stable-ng", "factory-eywa"}
)
#: Registries whose pools use the CryptoSwap ABI: `uint256` coin indices.
CRYPTO_REGISTRIES = frozenset(
    {"crypto", "factory-crypto", "factory-twocrypto", "factory-tricrypto"}
)


def _float(value: Any, default: float = 0.0) -> float:
    """Coerce an API number that may be a string, None, or already a float."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    """Coerce an API integer, which is usually a decimal string."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


@dataclass(slots=True)
class Coin:
    """One asset in a pool."""

    address: str
    symbol: str
    decimals: int
    usd_price: float = 0.0
    #: Pool-held balance in the coin's smallest unit.
    pool_balance: int = 0
    is_base_pool_lp: bool = False

    @property
    def balance(self) -> float:
        """Pool balance as a human number. Display only -- never for math."""
        return self.pool_balance / (10**self.decimals) if self.decimals else 0.0

    @property
    def balance_usd(self) -> float:
        return self.balance * self.usd_price

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Coin":
        return cls(
            address=raw.get("address") or "",
            symbol=raw.get("symbol") or "?",
            decimals=_int(raw.get("decimals"), 18),
            usd_price=_float(raw.get("usdPrice")),
            pool_balance=_int(raw.get("poolBalance")),
            is_base_pool_lp=bool(raw.get("isBasePoolLpToken")),
        )


@dataclass(slots=True)
class Incentive:
    """A non-CRV reward token streamed to a pool's gauge."""

    symbol: str
    token_address: str
    apr: float = 0.0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Incentive":
        return cls(
            symbol=raw.get("symbol") or "?",
            token_address=raw.get("tokenAddress") or "",
            apr=_float(raw.get("apy")),
        )


@dataclass(slots=True)
class Pool:
    """A Curve pool, as much of it as the list and detail views need."""

    address: str
    name: str
    symbol: str
    chain: str
    registry: str
    coins: list[Coin]
    lp_token: str = ""
    tvl: float = 0.0
    gauge: str = ""
    #: veCRV boost range: (unboosted, max-boost) APR in percent.
    crv_apr: tuple[float, float] = (0.0, 0.0)
    incentives: list[Incentive] = field(default_factory=list)
    is_meta: bool = False
    is_broken: bool = False
    amplification: int = 0
    virtual_price: int = 0
    #: Attached from `getVolumes`, which `getPools` does not carry.
    volume_24h: float = 0.0
    base_apr: float = 0.0

    # -- derived ----------------------------------------------------------

    @property
    def is_stableswap(self) -> bool:
        """Which exchange ABI this pool speaks.

        StableSwap declares coin indices as `int128` and CryptoSwap as
        `uint256`. Same argument values, different function selectors, so
        getting this wrong produces a call that simply reverts. The registry
        is the reliable discriminator -- pool bytecode is not introspectable
        from here.
        """
        if self.registry in CRYPTO_REGISTRIES:
            return False
        if self.registry in STABLE_REGISTRIES:
            return True
        # An unknown registry is far more likely to be a new stable factory
        # than a new crypto one, and stable is also the safer default: its
        # `get_dy` reverting is visible immediately in the UI.
        return True

    @property
    def n_coins(self) -> int:
        return len(self.coins)

    @property
    def coin_symbols(self) -> list[str]:
        return [c.symbol for c in self.coins]

    @property
    def has_gauge(self) -> bool:
        return bool(self.gauge)

    @property
    def incentives_apr(self) -> float:
        """Total rewards APR: max-boost CRV plus every incentive token.

        This is what the "incentives" sort orders by. Max boost rather than
        minimum because it is the number a depositor can actually reach, and
        it is what Curve's own UI shows as the top of its range.
        """
        return self.crv_apr[1] + sum(i.apr for i in self.incentives)

    @property
    def total_apr(self) -> float:
        return self.base_apr + self.incentives_apr

    # -- parsing ----------------------------------------------------------

    @classmethod
    def from_api(cls, raw: dict[str, Any], chain: str = "") -> "Pool":
        """Build from one entry of `getPools/*`'s `data.poolData`."""
        crv = raw.get("gaugeCrvApy") or []
        crv_min = _float(crv[0]) if len(crv) > 0 else 0.0
        crv_max = _float(crv[1]) if len(crv) > 1 else crv_min

        # The single-registry endpoint omits blockchainId/registryId; the
        # big/all/small variants include them. Prefer the payload, fall back
        # to what the caller already knows.
        return cls(
            address=raw.get("address") or "",
            name=raw.get("name") or "",
            symbol=raw.get("symbol") or "",
            chain=raw.get("blockchainId") or chain,
            registry=raw.get("registryId") or "",
            coins=[Coin.from_api(c) for c in raw.get("coins") or []],
            lp_token=raw.get("lpTokenAddress") or raw.get("address") or "",
            tvl=_float(raw.get("usdTotal")),
            gauge=raw.get("gaugeAddress") or "",
            crv_apr=(crv_min, crv_max),
            incentives=[
                Incentive.from_api(r)
                for r in raw.get("gaugeRewards") or []
                # CRV itself shows up here on some pools; it is already
                # counted in gaugeCrvApy and would otherwise be double-added.
                if (r.get("symbol") or "").upper() != "CRV"
            ],
            is_meta=bool(raw.get("isMetaPool")),
            is_broken=bool(raw.get("isBroken")),
            amplification=_int(raw.get("amplificationCoefficient")),
            virtual_price=_int(raw.get("virtualPrice")),
        )

    @property
    def display_name(self) -> str:
        """Something short and identifying, whatever the API gave us."""
        if self.symbol:
            return self.symbol
        name = self.name or ""
        for prefix in ("Curve.fi ", "Curve.fi Factory Plain Pool: ", "Curve "):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        return name or self.address[:10]


def attach_volumes(pools: list[Pool], volumes: list[dict[str, Any]]) -> list[Pool]:
    """Join `getVolumes` rows onto pools by address.

    Addresses are checksummed in both payloads, but they are matched
    lowercased anyway -- the two endpoints are generated by different code
    paths and the join is worth nothing if a casing change silently drops
    every row. Measured 382/382 on Ethereum.
    """
    by_address = {
        (row.get("address") or "").lower(): row for row in volumes if row.get("address")
    }
    for pool in pools:
        row = by_address.get(pool.address.lower())
        if row is None:
            continue
        pool.volume_24h = _float(row.get("volumeUSD"))
        # `getVolumes` reports percent already multiplied out; the older
        # getSubgraphData endpoint returns the same number as a fraction.
        pool.base_apr = _float(row.get("latestWeeklyApyPcent"))
    return pools
