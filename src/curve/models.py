"""The domain objects, parsed out of Curve's API shapes.

Everything here is a plain dataclass with no Flet and no network in sight,
which is what makes the sorting, formatting and math testable without a
running app. The API's own shapes leak into the `from_*` classmethods and
stop there.

These are built on the **Prices API v2** (`prices.curve.finance/v2`), which
returns TVL, volume, base APR, the CRV boost range and extra reward tokens
in a single object -- the v1 main API split those across two endpoints that
had to be joined by address. Two v2 quirks are handled here rather than
leaked upward:

  * `gauges` is a list of *objects* (`{address, is_killed}`) on the list
    endpoint and a list of *strings* on the detail endpoint;
  * the list endpoint omits `lp_token_address`, `balances` and per-coin
    `usd_price`, so a pool starts partial and is filled in by
    `merge_detail` when its page is opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: `pool_type`/`registry_type` values whose pools use the StableSwap ABI:
#: `int128` coin indices. Both the v2 spellings and the older hyphenated v1
#: registry ids are listed, so a Pool built from either source dispatches
#: correctly.
STABLE_POOL_TYPES = frozenset(
    {
        # v2
        "main", "factory", "crvusd", "stableswapng",
        # v1 registry ids
        "factory-crvusd", "factory-stable-ng", "factory-eywa",
    }
)
#: Pool types using the CryptoSwap ABI: `uint256` coin indices.
CRYPTO_POOL_TYPES = frozenset(
    {
        # v2
        "crypto", "factory_crypto", "factory_tricrypto", "twocryptong",
        # v1 registry ids
        "factory-crypto", "factory-tricrypto", "factory-twocrypto",
    }
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
    """One asset in a pool.

    `balance` is a human number, not smallest units: v2 reports pool
    reserves already scaled. It is display-only either way -- anything
    headed for calldata is parsed from user input as an integer.
    """

    address: str
    symbol: str
    decimals: int
    index: int = 0
    usd_price: float = 0.0
    balance: float = 0.0
    balance_usd: float = 0.0

    @classmethod
    def from_v2(cls, raw: dict[str, Any]) -> "Coin":
        return cls(
            address=raw.get("address") or "",
            symbol=raw.get("symbol") or "?",
            decimals=_int(raw.get("decimals"), 18),
            index=_int(raw.get("pool_index")),
            usd_price=_float(raw.get("usd_price")),
        )


@dataclass(slots=True)
class Incentive:
    """A non-CRV reward token streamed to a pool's gauge."""

    symbol: str
    token_address: str
    apr: float = 0.0

    @classmethod
    def from_v2(cls, raw: dict[str, Any]) -> "Incentive":
        return cls(
            symbol=raw.get("symbol") or "?",
            token_address=raw.get("address") or raw.get("token_address") or "",
            apr=_float(raw.get("apr")),
        )


def _first_live_gauge(raw: Any) -> str:
    """Pull a usable gauge address out of either shape v2 returns.

    The list endpoint gives `[{"address": …, "is_killed": false}]` and the
    detail endpoint gives `["0x…"]`. Killed gauges are skipped: they still
    accept deposits but pay nothing, so offering one to stake into would be
    actively misleading.
    """
    if not isinstance(raw, list):
        return ""
    for entry in raw:
        if isinstance(entry, str) and entry:
            return entry
        if isinstance(entry, dict) and not entry.get("is_killed"):
            address = entry.get("address")
            if address:
                return address
    return ""


@dataclass(slots=True)
class Pool:
    """A Curve pool, as much of it as the list and detail views need."""

    address: str
    name: str
    chain: str = ""
    chain_id: int = 0
    registry: str = ""
    coins: list[Coin] = field(default_factory=list)
    lp_token: str = ""
    tvl: float = 0.0
    volume_24h: float = 0.0
    base_apr: float = 0.0
    gauge: str = ""
    #: veCRV boost range: (unboosted, max-boost) APR in percent.
    crv_apr: tuple[float, float] = (0.0, 0.0)
    incentives: list[Incentive] = field(default_factory=list)
    #: Off-gauge campaign rewards, claimed via a merkle drop. v1 had no
    #: equivalent, so this column simply did not exist before.
    merkle_apr: float = 0.0
    is_meta: bool = False
    amplification: float = 0.0
    virtual_price: float = 0.0
    #: False until `merge_detail` has run: the list endpoint omits the LP
    #: token, the reserves and per-coin prices.
    detailed: bool = False

    # -- derived ----------------------------------------------------------

    @property
    def is_stableswap(self) -> bool:
        """Which exchange ABI this pool speaks.

        StableSwap declares coin indices as `int128` and CryptoSwap as
        `uint256`. Same argument values, different function selectors, and
        a wrong guess produces a call that returns empty data rather than
        reverting -- see `curve.pool`. The registry is the only reliable
        discriminator available from the API.
        """
        registry = (self.registry or "").lower()
        if registry in CRYPTO_POOL_TYPES:
            return False
        if registry in STABLE_POOL_TYPES:
            return True
        # An unknown type is far likelier to be a new stable factory than a
        # new crypto one, and a StableSwap `get_dy` that fails is visible
        # immediately in the UI rather than silently mispricing.
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
        """Total rewards APR: max-boost CRV, every incentive token, merkle.

        This is what the "incentives" sort orders by. Max boost rather than
        minimum because it is the number a depositor can actually reach, and
        it is the top of the range Curve's own UI prints.
        """
        return self.crv_apr[1] + sum(i.apr for i in self.incentives) + self.merkle_apr

    @property
    def total_apr(self) -> float:
        return self.base_apr + self.incentives_apr

    @property
    def display_name(self) -> str:
        """Something short and identifying, whatever the API gave us."""
        name = self.name or ""
        for prefix in (
            "Curve.fi Factory Plain Pool: ",
            "Curve.fi Factory USD Metapool: ",
            "Curve.fi ",
            "Curve ",
        ):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        return name or self.address[:10]

    # -- parsing ----------------------------------------------------------

    @classmethod
    def from_v2(cls, raw: dict[str, Any], chain: str = "") -> "Pool":
        """Build from one entry of the v2 `/pools/` list."""
        return cls(
            address=raw.get("address") or "",
            name=raw.get("name") or "",
            chain=chain,
            chain_id=_int(raw.get("chain_id")),
            registry=raw.get("pool_type") or raw.get("registry_type") or "",
            coins=[Coin.from_v2(c) for c in raw.get("coins") or []],
            lp_token=raw.get("lp_token_address") or "",
            tvl=_float(raw.get("tvl_usd")),
            volume_24h=_float(raw.get("trading_volume_24h")),
            # Weekly rather than daily: a single day's fees on a quiet pool
            # swing wildly, and weekly is what Curve's own list column shows.
            base_apr=_float(raw.get("base_weekly_apr")),
            gauge=_first_live_gauge(raw.get("gauges")),
            crv_apr=(_float(raw.get("crv_apr")), _float(raw.get("crv_apr_boosted"))),
            incentives=[
                Incentive.from_v2(r)
                for r in raw.get("extra_rewards_apr") or []
                # CRV shows up here on some pools; it is already counted in
                # crv_apr_boosted and would otherwise be added twice.
                if (r.get("symbol") or "").upper() != "CRV"
            ],
            merkle_apr=_float(raw.get("merkle_apr")),
            is_meta=bool(raw.get("is_metapool") or raw.get("metapool")),
        )

    def merge_detail(self, raw: dict[str, Any]) -> "Pool":
        """Fold in the extra fields only `/pools/{chain_id}/{address}` has.

        Mutates in place and returns self, so a cached list entry gains its
        detail once and keeps it. Everything here is absent from the list
        payload: without it there is no LP token to withdraw or stake, and
        no reserves to draw a composition table from.
        """
        self.lp_token = raw.get("lp_token_address") or self.lp_token or self.address
        self.registry = raw.get("registry_type") or raw.get("pool_type") or self.registry
        if raw.get("gauges"):
            self.gauge = _first_live_gauge(raw["gauges"]) or self.gauge

        detail_coins = raw.get("coins") or []
        balances = raw.get("balances") or []
        balances_usd = raw.get("balances_usd") or []
        if detail_coins:
            self.coins = [Coin.from_v2(c) for c in detail_coins]
        for index, coin in enumerate(self.coins):
            if index < len(balances):
                coin.balance = _float(balances[index])
            if index < len(balances_usd):
                coin.balance_usd = _float(balances_usd[index])

        metadata = raw.get("metadata") or {}
        self.amplification = _float(metadata.get("a"))
        self.virtual_price = _float(metadata.get("virtual_price"))
        self.detailed = True
        return self
