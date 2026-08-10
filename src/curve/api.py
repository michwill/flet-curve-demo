"""Clients for Curve's APIs.

Pool data comes from the **Prices API v2** (`prices.curve.finance/v2`) and
charts from **v1** (`prices.curve.finance/v1`), because v2 has no OHLC
endpoints. Neither needs a key.

Why v2 for pools: it returns TVL, volume, base APR, the CRV boost range,
extra reward tokens and merkle rewards in one object, where the v1 main API
split those across `getPools` and `getVolumes` and needed a join by address.
It also sorts, searches and filters server-side.

The one constraint that shapes this file: **`pagination` is capped at 50.**
There is no "give me everything" call, so the list is a cursor
(`PoolFeed`) that pulls a page at a time and lets the server do the
ordering -- see the note on `PoolFeed`.

See docs/curve-api.md for the full endpoint survey.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
from .models import Pool, _first_live_gauge
from .portfolio import Target
from .sort import get_sort

PRICES_V2 = "https://prices.curve.finance/v2"
PRICES_V1 = "https://prices.curve.finance/v1"

#: The v2 hard cap on `pagination`; anything larger is a 422.
MAX_PAGE_SIZE = 50

#: Per-pool detail requests in flight at once, when `pool_rates` has to
#: fall back to asking one at a time.
DETAIL_REQUESTS = 8

#: Below this the list is mostly dust: thousands of abandoned factory pools
#: with no liquidity. v1's `getPools/big` drew the line in the same place,
#: and it takes Ethereum from 2210 pools to 385.
DEFAULT_MIN_TVL = 10_000.0

#: Prices data is cached at the edge for ~5 minutes; match it.
CACHE_TTL = 300.0

#: How many candles to ask for, whatever their size. The window follows from
#: the candle: 200 x 15m is about two days, 200 x 1d is about seven months.
#: Curve's own chart works the same way -- you pick the candle, not the span.
CANDLE_COUNT = 200


@dataclass(slots=True, frozen=True)
class CandleSize:
    """One entry in the candle-size picker, and its API aggregation.

    `agg_units` is one of `minute`, `hour`, `day` -- the whole enum the
    Prices API accepts. Every combination below was checked against
    `lp_ohlc` and returns candles at exactly `seconds` apart.
    """

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
    """What a pool holds, in USD, from its own reserves.

    `balances_usd` rather than `tvl_usd`, and the difference is not
    academic. A position is priced by its share of what the pool holds,
    so a wrong total is multiplied by however small the supply is -- and
    on a pool drained to its rounding floor that factor is enormous.

    Two of them on Ethereum say so in one object: ETHx/wstETH reports
    `balances_usd` of `[6.9e-11, 7.8e-11]` and `tvl_usd` of `30.03`,
    which put a dust position that nobody can withdraw anything from at
    thirty dollars on the Portfolio page. The reserves agree with the
    chain to the wei; the total does not agree with anything.

    So the total is derived rather than taken. `tvl_usd` remains the
    fallback for a payload that carries no reserves at all, where a
    number of unknown provenance still beats no pool at all -- it decides
    only whether the pool is worth scanning, and every position in it is
    priced from this sum.
    """
    balances = raw.get("balances_usd")
    if isinstance(balances, list) and balances:
        return sum(float(value or 0.0) for value in balances)
    return float(raw.get("tvl_usd") or 0.0)


def _rates_key(chain_id: int, address: str) -> str:
    """Where one pool's published rates live in the cache.

    Separate from the `detail:` key even though a detail payload can fill
    it, because the two are not the same promise: this one says only that
    the APR fields are present, which is all the list endpoint carries.
    """
    return f"rates:{chain_id}:{address.lower()}"


class CurveApi:
    """Reads Curve's APIs, with a small time-based cache."""

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        #: Requests it would take to page each chain's pool list, learned
        #: from the last listing rather than asked for -- see `pool_rates`.
        self._pages: dict[int, int] = {}
        #: How many per-pool requests may be in flight at once. Enough that
        #: a wallet in a dozen pools is one wait, few enough that this is
        #: not a burst against somebody else's API.
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
            # FastAPI's error envelope, e.g. a 422 on a bad query value.
            raise ApiError(f"Curve API rejected {path}: {payload['detail']}")
        return payload

    async def chains(self) -> dict[str, int]:
        """Chain name -> numeric chain id, across both APIs.

        v2 addresses chains by id, not by the name v1 used, so this mapping
        is needed before any pool call. Worth reading rather than
        hardcoding: v2 currently covers 12 chains against v1's 21, and the
        list will move.

        Curve Lite chains are folded in here, so the picker offers one list
        and everything downstream takes a chain id. Where a name is in both
        -- Avalanche and Fantom are -- v2 wins, because it has volume and
        charts and the Lite entry has neither.
        """
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

    # -- Curve Lite -------------------------------------------------------

    async def lite_chains(self) -> dict[str, LiteChain]:
        """The Curve Lite deployments, or nothing if that API is down.

        Nothing rather than an error: a failure here should cost the Lite
        chains from the picker, not the whole list of chains.
        """
        cached = self._cached("lite:chains")
        if cached is not None:
            return cached
        try:
            payload = await get_json(f"{LITE_API}/get_platforms", timeout=LITE_TIMEOUT)
        except ApiError:
            return self._store("lite:chains", {})
        return self._store("lite:chains", parse_platforms(payload))

    async def lite_chain_ids(self) -> set[int]:
        """Which chain ids are served by the Lite API rather than v2.

        Only the ones v2 does not have: a chain in both is a full
        deployment that the Lite API also happens to list.
        """
        cached = self._cached("lite:ids")
        if cached is not None:
            return cached
        lite = await self.lite_chains()
        # `chains()` calls this, so read v2 directly rather than recursing.
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
        """Every pool on a Lite chain, in one request and then cached.

        There is no paging on this API and no need for one: the largest
        Lite chain is a couple of hundred pools, which is one response and
        the whole chain, so the list can be ordered and searched locally.
        """
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
        """One page of pools, plus the total count matching the filters.

        Ordering, searching and filtering are all done by the server: with
        a 50-row cap there is no way to sort correctly on the client
        without first pulling every page.
        """
        if await self.is_lite(chain_id):
            # Same contract, served from one cached response: a page and
            # the total count, ordered and filtered the way v2 would have.
            return select(
                await self._lite_pools(chain_id, chain),
                sort_local=get_sort(sort_by).local,
                direction=direction,
                search=search,
                min_tvl=LITE_MIN_TVL if min_tvl else None,
                page=page,
                page_size=min(page_size, MAX_PAGE_SIZE),
            )

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

        payload = await self._v2("/pools/", params)
        pools = [Pool.from_v2(raw, chain) for raw in payload.get("pools") or []]
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
        """What one token is worth, for pricing rewards.

        The pool payloads carry a price for every *incentive* token, so
        the only thing this is needed for is CRV -- which is paid by the
        Minter rather than streamed by the gauge and so appears in no
        pool's reward list. A v1 endpoint, because v2 has no equivalent.

        Zero when it cannot be had. A reward with no price is shown as an
        amount without a value, which is honest; refusing to show the
        amount because the price is missing would not be.
        """
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
        """One pool by address, without paging a list to find it.

        What a deep link needs: someone opens `/ethereum/0xC09e…` and the
        pool it names may be nowhere near the first page, or below the TVL
        floor, or on a chain whose list has not loaded yet.

        The two APIs answer differently. v2 has a detail endpoint that
        returns a whole pool, so it is read and merged in one go. Curve
        Lite has no such endpoint -- but it hands over the entire chain in
        one request anyway, and that response is already cached, so this
        is a lookup rather than a fetch.
        """
        if await self.is_lite(chain_id):
            for pool in await self._lite_pools(chain_id, chain):
                if pool.address.lower() == address.lower():
                    return pool
            raise ApiError(f"No pool at {address} on this network.")
        payload = await self.pool_detail(chain_id, address)
        if not isinstance(payload, dict) or not payload.get("address"):
            raise ApiError(f"No pool at {address} on this network.")
        pool = Pool.from_v2(payload, chain)
        return pool.merge_detail(payload)

    async def chain_totals(self, chain_id: int) -> dict[str, float | None]:
        """Headline TVL and volume for the chain, for the list header.

        v2 has no totals endpoint, so this comes from v1's per-chain
        summary, which reports them for every pool rather than just the
        ones above the TVL floor.

        A Lite chain has a TVL and no volume at all, and `None` is how that
        is said -- the header omits the clause rather than printing a zero
        that reads as "nobody traded today".
        """
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
        """Every pool worth asking about, with its LP token and gauge.

        This is the expensive half of a portfolio scan -- the chain part
        is under a second, see `curve.portfolio` -- so it is worth knowing
        where the requests go:

          * `/v1/chains/{chain}` lists every pool on the chain with its LP
            token and TVL, in one request. That is the only endpoint that
            carries the LP token for *all* of them, and it matters because
            an old-registry pool's LP token is a different contract from
            the pool. Roughly 2.4MB on Ethereum;
          * gauges come from v2, which pages at fifty, so that is twenty
            or so requests -- issued together rather than in sequence.

        A Lite chain needs neither: its own endpoint returns pools, LP
        tokens and gauges in one answer.

        Pools with no TVL at all are dropped. There are over a thousand of
        them on Ethereum -- more than half of everything ever deployed --
        and a balance in one is worth nothing by definition.
        """
        if await self.is_lite(chain_id):
            return [
                Target(
                    address=pool.address,
                    name=pool.display_name,
                    chain=chain,
                    lp_token=pool.lp_token or pool.address,
                    gauge=pool.gauge,
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
        """Pool address -> live gauge, for every pool on the chain.

        v2 is the only list that carries gauges and it pages at fifty, so
        this is twenty-odd requests. They go out together: in sequence it
        is the slowest thing on the page by a wide margin.
        """
        gauges: dict[str, str] = {}
        for raw in await self._list_pools(chain_id):
            gauge = _first_live_gauge(raw.get("gauges"))
            if gauge:
                gauges[(raw.get("address") or "").lower()] = gauge
        return gauges

    async def _list_pools(self, chain_id: int) -> list[dict[str, Any]]:
        """Every pool on the chain, as the list endpoint describes it.

        Each entry is kept under its own key on the way past, because the
        list carries the *published rates* -- `crv_apr`, `crv_apr_boosted`
        and `extra_rewards_apr`, the last complete with each token's
        address, decimals and price -- and those are exactly what the
        portfolio's earnings pass needs. Checked against the detail
        endpoint: identical fields, identical values.

        Whoever wanted the whole list has therefore already paid for every
        rate on the chain, and `pool_rates` below can answer from here
        rather than asking again per pool.
        """
        first = await self._list_page(chain_id, 1)
        rest = await asyncio.gather(
            *[
                self._list_page(chain_id, number)
                for number in range(2, self._pages[chain_id] + 1)
            ],
            return_exceptions=True,
        )
        pools = list(first)
        for page in rest:
            if isinstance(page, list):  # a page that failed; the rest still count
                pools += page
        return pools

    async def _list_page(self, chain_id: int, page: int) -> list[dict[str, Any]]:
        """One page of the list, with every rate on it kept.

        Also records what paging this chain would cost, which is the only
        thing `pool_rates` needs a request to find out and the reason it
        is worth spending one.
        """
        payload = await self._v2(
            "/pools/",
            {"chain_id": chain_id, "page": page, "pagination": MAX_PAGE_SIZE},
        )
        count = int(payload.get("count") or 0)
        # Rounded up, not `count // size + 1` -- which asks for one page
        # past the end whenever the count divides exactly, and got an
        # empty answer for the trouble.
        self._pages[chain_id] = max(1, -(-count // MAX_PAGE_SIZE))
        pools = list(payload.get("pools") or [])
        for raw in pools:
            self._store(_rates_key(chain_id, raw.get("address") or ""), raw)
        return pools

    async def pool_rates(
        self, chain_id: int, addresses: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """The published rates for these pools, in as few requests as it takes.

        Three routes, cheapest first:

          * **nothing** -- the portfolio scan lists every pool on the chain
            to find the gauges, and `_list_pools` keeps what it saw. On the
            page this actually runs on, that covers everything;
          * **the list** -- one request per page of the chain, which beats
            asking per pool once there are more pools to look up than the
            chain has pages. Ethereum is 45 pages against 300 positions;
          * **per pool** -- one request each, which is fewer when a wallet
            is in a handful of pools. Most wallets are.

        The choice is arithmetic, not a guess, and the one number it needs
        -- how many pages the chain has -- costs a request to learn. So it
        is only bought when a wallet is in enough pools that the answer
        could go either way, and the page it arrives on is kept and
        counted, leaving `pages - 1` as what the list route still owes.

        A pool nothing answers for is left out rather than raised: it costs
        that row its rate and no more.
        """
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
            # Never listed, and enough pools wanted that the list might
            # win. One page settles it -- and fills fifty rates whichever
            # way it goes, so it is not wasted even when it loses.
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

    # -- v1: charts -------------------------------------------------------
    #
    # v2 has no OHLC endpoints at all, so these stay on v1. Note the paths
    # are top level, not nested under /pools/.

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
        """Candles for `base` priced in `quote`, within a single pool.

        Named the way a pair is read: "WBTC/USDC" is the price of WBTC in
        USDC, so `base` is WBTC and `quote` is USDC. Both are coin
        addresses; the pool address only says which market to read them
        from.

        **The endpoint's own parameters are the other way round.** It
        returns the price of `reference_token` *denominated in*
        `main_token`, so the quote coin goes in `main_token`. Measured
        rather than assumed, on tricryptoUSDC: `main_token=WBTC,
        reference_token=USDC` answers 0.0000156, and swapping them answers
        63,586. Reading the names as base/quote is the natural mistake and
        it inverts every pair chart.
        """
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


class PoolFeed:
    """A paginated, server-ordered cursor over one chain's pools.

    v2 caps a page at 50 rows, so the alternatives were to pull every page
    up front (eight requests for Ethereum, before the first row paints) or
    to page as the list scrolls. This is the second: the first page appears
    after one request and the rest arrive as they are needed.

    The consequence, and the reason ordering moved server-side: a client
    cannot sort a list it has not fully loaded. So changing the sort or the
    search resets the cursor and asks the server again, which also means
    the top of the list is always the true top, not the top of whatever
    happened to be in memory.

    `generation` guards against a reset landing mid-flight: a page that
    comes back for a superseded query is discarded rather than appended to
    the wrong list.
    """

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
        #: A Curve Lite chain: paged from one cached response rather than
        #: from the server, and with no volume or base APR to show. The
        #: view reads this to decide which columns exist at all.
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
        # Anything already in flight belongs to the previous query.
        self._generation += 1

    # -- loading ----------------------------------------------------------

    async def load_more(self) -> list[Pool]:
        """Fetch the next page and append it. Returns the new pools only.

        Safe to call spuriously -- a scroll handler fires often -- and
        returns an empty list when a load is already running, when the feed
        is exhausted, or when the query changed underneath it.
        """
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
        # A short page means the server has nothing more, whatever the
        # count said -- treat it as the end rather than paging forever.
        if not pools:
            self.total = len(self.pools)
            return []
        self.pools.extend(pools)
        return pools
