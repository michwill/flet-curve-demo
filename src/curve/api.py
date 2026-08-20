"""Clients for Curve's APIs."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .external import (
    EXTERNAL_BASE,
    MANIFEST,
    ExternalCampaign,
    by_pool,
    parse_campaign,
    parse_manifest,
)
from .http import ApiError, build_url, get_json
from .lite import (
    LITE_API,
    LITE_MIN_TVL,
    LITE_TIMEOUT,
    LiteChain,
    parse_hidden,
    parse_platforms,
    parse_pools,
    select,
)
from .merkl import (
    MAX_ITEMS,
    MERKL_API,
    MerklCampaign,
    by_identifier,
    parse_opportunities,
    parse_tokens,
    underlying_ids,
    with_underlying,
)
from .models import Pool, _first_dead_gauge, _first_live_gauge, _float
from .portfolio import Target
from .sort import get_sort

PRICES_V2 = "https://prices.curve.finance/v2"
PRICES_V1 = "https://prices.curve.finance/v1"

#: The v2 hard cap on `pagination`; anything larger is a 422.
MAX_PAGE_SIZE = 50

#: Per-pool detail requests in flight at once, when `pool_rates` has to fall
#: back to asking one at a time.
DETAIL_REQUESTS = 8

#: Below this the list is mostly dust: thousands of abandoned factory pools
#: with no liquidity.
DEFAULT_MIN_TVL = 10_000.0

#: Prices data is cached at the edge for ~5 minutes; match it.
CACHE_TTL = 300.0

#: How long to wait on Merkl or on GitHub before doing without them.
CAMPAIGN_TIMEOUT = 5.0

#: Pages of Merkl opportunities to walk before deciding something is wrong.
MERKL_MAX_PAGES = 5

#: How many candles to ask for, whatever their size.
CANDLE_COUNT = 200

#: How many trades or liquidity events a table shows. The v1 endpoints cap
#: `per_page` at 100.
ACTIVITY_ROWS = 40


@dataclass(slots=True, frozen=True)
class CandleSize:
    """One entry in the candle-size picker, and its API aggregation."""

    label: str
    agg_number: int
    agg_units: str
    seconds: int

    def window(self, count: int = CANDLE_COUNT) -> int:
        """How far back to ask, in seconds, for `count` of these candles."""
        return self.seconds * count


CANDLE_SIZES: tuple[CandleSize, ...] = (
    CandleSize("15m", 15, "minute", 900),
    CandleSize("30m", 30, "minute", 1800),
    CandleSize("1h", 1, "hour", 3600),
    CandleSize("4h", 4, "hour", 14400),
    CandleSize("6h", 6, "hour", 21600),
    CandleSize("12h", 12, "hour", 43200),
    CandleSize("1d", 1, "day", 86400),
    CandleSize("7d", 7, "day", 604800),
    CandleSize("14d", 14, "day", 1209600),
)

#: Matches what Curve's own pool chart opens on.
DEFAULT_CANDLE_SIZE = "1d"

_SIZES_BY_LABEL = {size.label: size for size in CANDLE_SIZES}


def get_candle_size(label: str) -> CandleSize:
    """Look up a candle size by label, falling back to the default."""
    return _SIZES_BY_LABEL.get(label, _SIZES_BY_LABEL[DEFAULT_CANDLE_SIZE])


@dataclass(slots=True, frozen=True)
class Candle:
    """One OHLC bar. `time` is Unix seconds."""

    time: int
    open: float
    high: float
    low: float
    close: float

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Candle:
        return cls(
            time=int(raw.get("time") or 0),
            open=float(raw.get("open") or 0.0),
            high=float(raw.get("high") or 0.0),
            low=float(raw.get("low") or 0.0),
            close=float(raw.get("close") or 0.0),
        )

    @property
    def rising(self) -> bool:
        return self.close >= self.open


def pool_composition(raw: dict[str, Any]) -> float:
    """What a pool holds, in USD, from its own reserves."""
    balances = raw.get("balances_usd")
    if isinstance(balances, list) and balances:
        return sum(float(value or 0.0) for value in balances)
    return float(raw.get("tvl_usd") or 0.0)


def _rates_key(chain_id: int, address: str) -> str:
    """Where one pool's published rates live in the cache."""
    return f"rates:{chain_id}:{address.lower()}"


class CurveApi:
    """Reads Curve's APIs, with a small time-based cache."""

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._pages: dict[int, int] = {}
        self._details = asyncio.Semaphore(DETAIL_REQUESTS)

    # -- caching ----------------------------------------------------------

    def _cached(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        stamped, value = hit
        if time.monotonic() - stamped > self._ttl:
            del self._cache[key]
            return None
        return value

    def _store(self, key: str, value: Any) -> Any:
        self._cache[key] = (time.monotonic(), value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    # -- v2: pools --------------------------------------------------------

    async def _v2(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await get_json(build_url(PRICES_V2, path, params))
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected response shape from {path}")
        if "detail" in payload and "data" not in payload and "pools" not in payload:
            raise ApiError(f"Curve API rejected {path}: {payload['detail']}")
        return payload

    async def chains(self) -> dict[str, int]:
        """Chain name -> numeric chain id, across both APIs."""
        cached = self._cached("chains")
        if cached is not None:
            return cached
        payload = await self._v2("/pools/chains/")
        mapping = {
            entry["name"]: int(entry["chain_id"])
            for entry in payload.get("data") or []
            if entry.get("name") and entry.get("chain_id") is not None
        }
        for name, chain in (await self.lite_chains()).items():
            mapping.setdefault(name, chain.chain_id)
        return self._store("chains", mapping)

    async def chain_tvls(self) -> dict[str, float]:
        """Chain name -> what is in its pools, for every chain at once.

        One request for the ten v2 chains, and the Lite deployments list
        for the other seventeen -- which is the whole reason this is worth
        having: the per-chain totals endpoint answers with the chain's
        entire pool list attached, 2.3 MB for Ethereum, so asking it
        twenty-six times to order a menu is not a trade worth making. See
        `chain_totals`, which is the one that pays that price, once, for
        the chain actually on screen.

        `pool_tvl` rather than the lending TVL beside it: the picker is a
        list of places to swap.
        """
        cached = self._cached("chain_tvls")
        if cached is not None:
            return cached
        totals: dict[str, float] = {}
        try:
            payload = await get_json(build_url(PRICES_V1, "/chains/"))
        except ApiError:
            payload = {}
        for entry in (payload or {}).get("data") or []:
            name = entry.get("name")
            if name:
                totals[str(name)] = _float(entry.get("pool_tvl"))
        for name, chain in (await self.lite_chains()).items():
            totals.setdefault(name, float(chain.tvl or 0.0))
        return self._store("chain_tvls", totals)

    # -- Curve Lite -------------------------------------------------------

    async def lite_chains(self) -> dict[str, LiteChain]:
        """The Curve Lite deployments, or nothing if that API is down."""
        cached = self._cached("lite:chains")
        if cached is not None:
            return cached
        try:
            payload = await get_json(f"{LITE_API}/get_platforms", timeout=LITE_TIMEOUT)
        except ApiError:
            return self._store("lite:chains", {})
        return self._store("lite:chains", parse_platforms(payload))

    async def lite_chain_ids(self) -> set[int]:
        """Which chain ids are served by the Lite API rather than v2."""
        cached = self._cached("lite:ids")
        if cached is not None:
            return cached
        lite = await self.lite_chains()
        payload = await self._v2("/pools/chains/")
        big = {
            int(entry["chain_id"])
            for entry in payload.get("data") or []
            if entry.get("chain_id") is not None
        }
        ids = {chain.chain_id for chain in lite.values()} - big
        return self._store("lite:ids", ids)

    async def is_lite(self, chain_id: int) -> bool:
        return chain_id in await self.lite_chain_ids()

    async def _lite_pools(self, chain_id: int, chain: str) -> list[Pool]:
        """Every pool on a Lite chain, in one request and then cached."""
        key = f"lite:pools:{chain_id}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await get_json(f"{LITE_API}/get_pools/{chain_id}")
        pools = parse_pools(payload, chain or "", await self._lite_hidden())
        return self._store(key, pools)

    async def _lite_hidden(self) -> set[tuple[int, str]]:
        cached = self._cached("lite:hidden")
        if cached is not None:
            return cached
        try:
            payload = await get_json(f"{LITE_API}/get_hidden_pools")
        except ApiError:
            return self._store("lite:hidden", set())
        return self._store("lite:hidden", parse_hidden(payload))

    # -- campaigns Curve does not publish ----------------------------------
    # Two more sources, neither of them Curve's: Merkl, which pays both
    # staked and unstaked liquidity and is the only place points campaigns
    # are reported at all, and the `external-rewards` directory in curve-
    # frontend, which is the only record of the rest.

    async def merkl_campaigns(self, chain_id: int) -> dict[str, list[MerklCampaign]]:
        """Live Merkl campaigns on this chain, keyed by what they watch."""
        key = f"merkl:{chain_id}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        found: list[MerklCampaign] = []
        for page in range(MERKL_MAX_PAGES):
            try:
                payload = await get_json(
                    build_url(
                        MERKL_API,
                        "/opportunities",
                        {
                            "chainId": chain_id,
                            "mainProtocolId": "curve",
                            "status": "LIVE",
                            "items": MAX_ITEMS,
                            "page": page,
                        },
                    ),
                    timeout=CAMPAIGN_TIMEOUT,
                )
            except ApiError:
                break
            found += parse_opportunities(payload)
            if not isinstance(payload, list) or len(payload) < MAX_ITEMS:
                break
        return self._store(key, by_identifier(await self._unwrap(found)))

    async def _unwrap(self, campaigns: list[MerklCampaign]) -> list[MerklCampaign]:
        """Find out what the wrapper-denominated campaigns actually pay."""
        wanted = sorted(underlying_ids(campaigns))
        if not wanted:
            return campaigns
        try:
            payload = await get_json(
                build_url(MERKL_API, "/tokens", {"id": wanted}),
                timeout=CAMPAIGN_TIMEOUT,
            )
        except ApiError:
            return campaigns
        return with_underlying(campaigns, parse_tokens(payload))

    async def external_campaigns(
        self,
    ) -> dict[tuple[str, str], list[ExternalCampaign]]:
        """curve-frontend's point campaigns, keyed by `(chain, address)`."""
        key = "external:campaigns"
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            manifest = await get_json(MANIFEST, timeout=CAMPAIGN_TIMEOUT)
        except ApiError:
            return self._store(key, {})

        async def one(name: str) -> list[ExternalCampaign]:
            try:
                payload = await get_json(
                    f"{EXTERNAL_BASE}/campaigns/{name}", timeout=CAMPAIGN_TIMEOUT
                )
            except ApiError:
                return []  # one platform missing, not the whole directory
            return parse_campaign(payload)

        files = parse_manifest(manifest)
        found = [
            campaign
            for group in await asyncio.gather(*(one(name) for name in files))
            for campaign in group
        ]
        return self._store(key, by_pool(found))

    async def _campaign_indexes(
        self, chain_id: int
    ) -> tuple[dict[str, list[MerklCampaign]], dict[tuple[str, str], list[ExternalCampaign]]]:
        merkl, external = await asyncio.gather(
            self.merkl_campaigns(chain_id), self.external_campaigns()
        )
        return merkl, external

    async def attach_campaigns(
        self, chain_id: int, chain: str, pools: Sequence[Pool]
    ) -> None:
        """Fill in `pool.merkl` and `pool.points` for these pools."""
        if not pools:
            return
        merkl, external = await self._campaign_indexes(chain_id)
        for pool in pools:
            pool.attach_campaigns(merkl, external, chain=chain)

    async def list_pools(
        self,
        chain_id: int,
        *,
        chain: str = "",
        page: int = 1,
        page_size: int = MAX_PAGE_SIZE,
        sort_by: str = "volume",
        direction: str = "desc",
        search: str = "",
        min_tvl: float | None = DEFAULT_MIN_TVL,
    ) -> tuple[list[Pool], int]:
        """One page of pools, plus the total count matching the filters."""
        if await self.is_lite(chain_id):
            listing, campaigns = await asyncio.gather(
                self._lite_pools(chain_id, chain), self._campaign_indexes(chain_id)
            )
            for pool in listing:
                pool.attach_campaigns(*campaigns, chain=chain)
            pools, total = select(
                listing,
                sort_local=get_sort(sort_by).local,
                direction=direction,
                search=search,
                min_tvl=LITE_MIN_TVL if min_tvl else None,
                page=page,
                page_size=min(page_size, MAX_PAGE_SIZE),
            )
            return pools, total

        params: dict[str, Any] = {
            "chain_id": chain_id,
            "page": max(1, page),
            "pagination": min(page_size, MAX_PAGE_SIZE),
            "sort_by": sort_by,
            "sort_direction": direction,
        }
        if min_tvl:
            params["min_tvl"] = min_tvl
        if search:
            params["search_string"] = search

        payload, campaigns = await asyncio.gather(
            self._v2("/pools/", params), self._campaign_indexes(chain_id)
        )
        pools = [Pool.from_v2(raw, chain) for raw in payload.get("pools") or []]
        for pool in pools:
            pool.attach_campaigns(*campaigns, chain=chain)
        return pools, int(payload.get("count") or 0)

    async def pool_detail(self, chain_id: int, address: str) -> dict[str, Any]:
        """The fields the list endpoint omits: LP token, reserves, prices."""
        key = f"detail:{chain_id}:{address.lower()}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._v2(f"/pools/{chain_id}/{address}")
        return self._store(key, payload)

    async def usd_price(self, chain: str, address: str) -> float:
        """What one token is worth, for pricing rewards."""
        key = f"price:{chain}:{address.lower()}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            payload = await self._v1(f"/usd_price/{chain}/{address}")
        except ApiError:
            return 0.0
        price = float(((payload or {}).get("data") or {}).get("usd_price") or 0.0)
        return self._store(key, price)

    async def get_pool(self, chain_id: int, address: str, chain: str = "") -> Pool:
        """One pool by address, without paging a list to find it."""
        if await self.is_lite(chain_id):
            for pool in await self._lite_pools(chain_id, chain):
                if pool.address.lower() == address.lower():
                    await self.attach_campaigns(chain_id, chain, [pool])
                    return pool
            raise ApiError(f"No pool at {address} on this network.")
        payload = await self.pool_detail(chain_id, address)
        if not isinstance(payload, dict) or not payload.get("address"):
            raise ApiError(f"No pool at {address} on this network.")
        pool = Pool.from_v2(payload, chain)
        pool.merge_detail(payload)
        await self.attach_campaigns(chain_id, chain, [pool])
        return pool

    async def chain_totals(self, chain_id: int) -> dict[str, float | None]:
        """Headline TVL and volume for the chain, for the list header."""
        key = f"totals:{chain_id}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        if await self.is_lite(chain_id):
            for chain in (await self.lite_chains()).values():
                if chain.chain_id == chain_id:
                    return self._store(key, {"tvl": chain.tvl, "volume": None})
            return {"tvl": 0.0, "volume": None}
        try:
            chains = {v: k for k, v in (await self.chains()).items()}
            name = chains.get(chain_id, "ethereum")
            payload = await get_json(build_url(PRICES_V1, f"/chains/{name}"))
            totals = (payload or {}).get("total") or {}
            return self._store(
                key,
                {
                    "tvl": float(totals.get("total_tvl") or 0.0),
                    "volume": float(totals.get("trading_volume_24h") or 0.0),
                },
            )
        except (ApiError, KeyError, TypeError, ValueError):
            return {"tvl": 0.0, "volume": 0.0}

    async def portfolio_targets(self, chain: str, chain_id: int) -> list[Target]:
        """Every pool worth asking about, with its LP token and gauge."""
        if await self.is_lite(chain_id):
            return [
                Target(
                    address=pool.address,
                    name=pool.display_name,
                    chain=chain,
                    lp_token=pool.lp_token or pool.address,
                    gauge=pool.any_gauge,
                    tvl=pool.tvl,
                    coins=tuple(
                        (coin.address, coin.symbol) for coin in pool.display_coins
                    ),
                )
                for pool in await self._lite_pools(chain_id, chain)
                if pool.tvl > 0
            ]

        listing, gauges = await asyncio.gather(
            get_json(build_url(PRICES_V1, f"/chains/{chain}"), timeout=120.0),
            self._all_gauges(chain_id),
        )
        targets = []
        for raw in (listing or {}).get("data") or []:
            held = pool_composition(raw)
            if held <= 0:
                continue
            address = raw.get("address") or ""
            targets.append(
                Target(
                    address=address,
                    name=raw.get("name") or "",
                    chain=chain,
                    lp_token=raw.get("lp_token_address") or address,
                    gauge=gauges.get(address.lower(), ""),
                    tvl=held,
                    coins=tuple(
                        (coin.get("address") or "", coin.get("symbol") or "?")
                        for coin in raw.get("coins") or []
                    ),
                )
            )
        return targets

    async def _all_gauges(self, chain_id: int) -> dict[str, str]:
        """Pool address -> the gauge to read balances from.

        Live where there is one, and a killed one otherwise: this feeds
        the portfolio scan, and a killed gauge that still holds somebody's
        LP is exactly the balance they most need to see.
        """
        gauges: dict[str, str] = {}
        for raw in await self._list_pools(chain_id):
            gauge = _first_live_gauge(raw.get("gauges")) or _first_dead_gauge(
                raw.get("gauges")
            )
            if gauge:
                gauges[(raw.get("address") or "").lower()] = gauge
        return gauges

    async def _list_pools(self, chain_id: int) -> list[dict[str, Any]]:
        """Every pool on the chain, as the list endpoint describes it.

        A page that fails is asked for a second time, and if it fails
        again the whole call raises. Silently returning the pages that did
        arrive is what this used to do, and it is a bad answer for the
        caller that matters: `portfolio_targets` reads this list, so a
        dropped page is a pool nobody asks about, and a deposit in it is
        reported as no deposit at all.
        """
        first = await self._list_page(chain_id, 1)
        numbers = list(range(2, self._pages[chain_id] + 1))
        rest = await asyncio.gather(
            *[self._list_page(chain_id, number) for number in numbers],
            return_exceptions=True,
        )
        retries = [n for n, page in zip(numbers, rest, strict=True)
                   if not isinstance(page, list)]
        if retries:
            second = await asyncio.gather(
                *[self._list_page(chain_id, number) for number in retries],
                return_exceptions=True,
            )
            missing = [n for n, page in zip(retries, second, strict=True)
                       if not isinstance(page, list)]
            if missing:
                raise ApiError(
                    f"Curve's API did not serve page{'s' if len(missing) > 1 else ''} "
                    f"{', '.join(str(n) for n in missing)} of this chain's pools."
                )
            rest = [page for page in rest if isinstance(page, list)] + list(second)
        pools = list(first)
        for page in rest:
            if isinstance(page, list):
                pools += page
        return pools

    async def _list_page(self, chain_id: int, page: int) -> list[dict[str, Any]]:
        """One page of the list, with every rate on it kept."""
        payload = await self._v2(
            "/pools/",
            {"chain_id": chain_id, "page": page, "pagination": MAX_PAGE_SIZE},
        )
        count = int(payload.get("count") or 0)
        self._pages[chain_id] = max(1, -(-count // MAX_PAGE_SIZE))
        pools = list(payload.get("pools") or [])
        for raw in pools:
            self._store(_rates_key(chain_id, raw.get("address") or ""), raw)
        return pools

    async def pool_rates(
        self, chain_id: int, addresses: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """The published rates for these pools, in as few requests as it takes."""
        wanted = [address.lower() for address in addresses]

        def known() -> dict[str, dict[str, Any]]:
            return {
                address: cached
                for address in wanted
                if (cached := self._cached(_rates_key(chain_id, address))) is not None
            }

        rates = known()
        missing = [address for address in wanted if address not in rates]
        if not missing:
            return rates

        pages = self._pages.get(chain_id)
        first: list[dict[str, Any]] | None = None
        if pages is None and len(missing) >= MAX_PAGE_SIZE:
            first = await self._list_page(chain_id, 1)
            rates = known()
            missing = [address for address in wanted if address not in rates]
            pages = self._pages[chain_id] - 1

        if missing and pages is not None and len(missing) > pages:
            await asyncio.gather(
                *[
                    self._list_page(chain_id, number)
                    for number in range(
                        2 if first is not None else 1, self._pages[chain_id] + 1
                    )
                ],
                return_exceptions=True,
            )
            rates = known()
            missing = [address for address in wanted if address not in rates]

        async def one(address: str) -> tuple[str, dict[str, Any] | None]:
            async with self._details:
                try:
                    return address, await self.pool_detail(chain_id, address)
                except ApiError:
                    return address, None

        for address, payload in await asyncio.gather(*(one(a) for a in missing)):
            if payload is not None:
                rates[address] = self._store(_rates_key(chain_id, address), payload)
        return rates

    # -- v1: charts --------------------------------------------------------
    # v2 has no OHLC endpoints at all, so these stay on v1.

    async def _v1(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await get_json(build_url(PRICES_V1, path, params))
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected response shape from {path}")
        return payload

    async def lp_candles(
        self,
        chain: str,
        pool: str,
        *,
        size: CandleSize,
        count: int = CANDLE_COUNT,
        now: int | None = None,
    ) -> list[Candle]:
        """Candles for the pool's LP token price, at the given candle size."""
        end = int(now if now is not None else time.time())
        key = f"lp_ohlc:{chain}:{pool}:{size.label}:{count}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._v1(
            f"/lp_ohlc/{chain}/{pool}",
            {
                "start": end - size.window(count),
                "end": end,
                "agg_number": size.agg_number,
                "agg_units": size.agg_units,
            },
        )
        candles = [Candle.from_api(c) for c in payload.get("data") or []]
        return self._store(key, candles)

    async def pair_candles(
        self,
        chain: str,
        pool: str,
        base: str,
        quote: str,
        *,
        size: CandleSize,
        count: int = CANDLE_COUNT,
        now: int | None = None,
    ) -> list[Candle]:
        """Candles for `base` priced in `quote`, within a single pool."""
        end = int(now if now is not None else time.time())
        key = f"ohlc:{chain}:{pool}:{base}:{quote}:{size.label}:{count}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._v1(
            f"/ohlc/{chain}/{pool}",
            {
                "main_token": quote,
                "reference_token": base,
                "start": end - size.window(count),
                "end": end,
                "agg_number": size.agg_number,
                "agg_units": size.agg_units,
            },
        )
        candles = [Candle.from_api(c) for c in payload.get("data") or []]
        return self._store(key, candles)

    # -- v1: what went through the pool -----------------------------------

    async def trades(
        self, chain: str, pool: str, tokens: Sequence[str], *, count: int = ACTIVITY_ROWS
    ) -> list[Trade]:
        """The newest swaps through a pool, across every pair it holds.

        The endpoint answers for one pair at a time -- three calls for a
        three-coin pool, six for four coins -- so they go out together and
        the answers are merged newest first. A pair that fails is left out
        rather than taking the table with it; all of them failing is an
        error, because then there is nothing to show and a reason for it.
        """
        addresses = list(dict.fromkeys(a.lower() for a in tokens if a))
        pairs = [
            (main, reference)
            for i, main in enumerate(addresses)
            for reference in addresses[i + 1 :]
        ]
        if not pairs:
            return []
        key = f"trades:{chain}:{pool}:{count}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        answers = await asyncio.gather(
            *(self._pair_trades(chain, pool, main, ref, count) for main, ref in pairs),
            return_exceptions=True,
        )
        failures = [a for a in answers if isinstance(a, BaseException)]
        if len(failures) == len(answers):
            raise failures[0] if isinstance(failures[0], ApiError) else ApiError(
                f"Could not read trades for {pool}: {failures[0]}"
            )
        trades = [t for a in answers if isinstance(a, list) for t in a]
        trades.sort(key=lambda trade: trade.time, reverse=True)
        return self._store(key, trades[:count])

    async def _pair_trades(
        self, chain: str, pool: str, main: str, reference: str, count: int
    ) -> list[Trade]:
        """One pair's swaps, in both directions."""
        payload = await self._v1(
            f"/trades/{chain}/{pool}",
            {
                "main_token": main,
                "reference_token": reference,
                "page": 1,
                "per_page": count,
            },
        )
        head = payload.get("main_token") or {}
        tail = payload.get("reference_token") or {}
        return [Trade.from_api(t, head, tail) for t in payload.get("data") or []]

    async def liquidity(
        self, chain: str, pool: str, *, count: int = ACTIVITY_ROWS
    ) -> list[LiquidityEvent]:
        """The newest deposits and withdrawals, newest first."""
        key = f"liquidity:{chain}:{pool}:{count}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._v1(
            f"/liquidity/{chain}/{pool}", {"page": 1, "per_page": count}
        )
        events = [LiquidityEvent.from_api(e) for e in payload.get("data") or []]
        return self._store(key, events)


def _stamp(raw: Any) -> int:
    """The v1 timestamps -- naive ISO, UTC -- as Unix seconds."""
    try:
        return int(datetime.fromisoformat(str(raw)).replace(tzinfo=UTC).timestamp())
    except (TypeError, ValueError):
        return 0


def _side(index: Any, main: dict, reference: dict) -> dict:
    """Which half of the pair a `sold_id` or `bought_id` names."""
    for token in (main, reference):
        if index is not None and index in (
            token.get("pool_index"),
            token.get("event_index"),
        ):
            return token
    return main


@dataclass(frozen=True)
class Trade:
    """One swap through a pool, as a row reads it."""

    time: int
    tx: str
    trader: str
    sold: str
    sold_address: str
    sold_amount: float
    bought: str
    bought_address: str
    bought_amount: float

    @classmethod
    def from_api(cls, raw: dict[str, Any], main: dict, reference: dict) -> Trade:
        sold = _side(raw.get("sold_id"), main, reference)
        bought = _side(raw.get("bought_id"), main, reference)
        return cls(
            time=_stamp(raw.get("time")),
            tx=str(raw.get("transaction_hash") or ""),
            trader=str(raw.get("buyer") or ""),
            sold=str(sold.get("symbol") or ""),
            sold_address=str(sold.get("address") or ""),
            sold_amount=_float(raw.get("tokens_sold")),
            bought=str(bought.get("symbol") or ""),
            bought_address=str(bought.get("address") or ""),
            bought_amount=_float(raw.get("tokens_bought")),
        )


@dataclass(frozen=True)
class LiquidityEvent:
    """One deposit into, or withdrawal from, a pool."""

    time: int
    tx: str
    provider: str
    added: bool
    #: Aligned with the pool's own coins, and zero for the ones untouched.
    amounts: tuple[float, ...]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> LiquidityEvent:
        kind = str(raw.get("liquidity_event_type") or "")
        return cls(
            time=_stamp(raw.get("time")),
            tx=str(raw.get("transaction_hash") or ""),
            provider=str(raw.get("provider") or ""),
            added=kind.startswith("Add"),
            amounts=tuple(_float(a) for a in raw.get("token_amounts") or ()),
        )


class PoolFeed:
    """A paginated, server-ordered cursor over one chain's pools."""

    def __init__(
        self,
        api: CurveApi,
        chain: str,
        chain_id: int,
        *,
        sort_by: str = "volume",
        direction: str = "desc",
        search: str = "",
        min_tvl: float | None = DEFAULT_MIN_TVL,
        lite: bool = False,
    ) -> None:
        self.api = api
        self.chain = chain
        self.chain_id = chain_id
        self.lite = lite
        self.sort_by = sort_by
        self.direction = direction
        self.search = search
        self.min_tvl = min_tvl

        self.pools: list[Pool] = []
        self.total: int | None = None
        self.loading = False
        self.error: str = ""
        self._page = 0
        self._generation = 0

    # -- state ------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        """True once every matching pool has been loaded."""
        if self.total is None:
            return False
        return len(self.pools) >= self.total

    @property
    def loaded(self) -> int:
        return len(self.pools)

    def reset(
        self,
        *,
        sort_by: str | None = None,
        direction: str | None = None,
        search: str | None = None,
    ) -> None:
        """Change the query and drop everything loaded so far."""
        if sort_by is not None:
            self.sort_by = sort_by
        if direction is not None:
            self.direction = direction
        if search is not None:
            self.search = search
        self.pools = []
        self.total = None
        self.error = ""
        self._page = 0
        self._generation += 1

    # -- loading ----------------------------------------------------------

    async def load_more(self) -> list[Pool]:
        """Fetch the next page and append it. Returns the new pools only."""
        if self.loading or self.exhausted:
            return []
        generation = self._generation
        self.loading = True
        try:
            page = self._page + 1
            pools, total = await self.api.list_pools(
                self.chain_id,
                chain=self.chain,
                page=page,
                sort_by=self.sort_by,
                direction=self.direction,
                search=self.search,
                min_tvl=self.min_tvl,
            )
        except ApiError as exc:
            if generation == self._generation:
                self.error = str(exc)
            return []
        finally:
            self.loading = False

        if generation != self._generation:
            return []  # superseded by a reset while we were waiting

        self._page = page
        self.total = total
        if not pools:
            self.total = len(self.pools)
            return []
        self.pools.extend(pools)
        return pools
