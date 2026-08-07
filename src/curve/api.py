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

import time
from dataclasses import dataclass
from typing import Any

from .http import ApiError, build_url, get_json
from .lite import LITE_API, LITE_MIN_TVL, LiteChain, parse_hidden, parse_platforms, parse_pools, select
from .models import Pool
from .sort import get_sort

PRICES_V2 = "https://prices.curve.finance/v2"
PRICES_V1 = "https://prices.curve.finance/v1"

#: The v2 hard cap on `pagination`; anything larger is a 422.
MAX_PAGE_SIZE = 50

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
    def from_api(cls, raw: dict[str, Any]) -> "Candle":
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


class CurveApi:
    """Reads Curve's APIs, with a small time-based cache."""

    def __init__(self, ttl: float = CACHE_TTL) -> None:
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}

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
            payload = await get_json(f"{LITE_API}/get_platforms")
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
        main_token: str,
        reference_token: str,
        *,
        size: CandleSize,
        count: int = CANDLE_COUNT,
        now: int | None = None,
    ) -> list[Candle]:
        """Candles for one coin priced in another, within a single pool.

        `main_token`/`reference_token` are coin addresses; the pool address
        only says which market to read them from.
        """
        end = int(now if now is not None else time.time())
        key = f"ohlc:{chain}:{pool}:{main_token}:{reference_token}:{size.label}:{count}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._v1(
            f"/ohlc/{chain}/{pool}",
            {
                "main_token": main_token,
                "reference_token": reference_token,
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
