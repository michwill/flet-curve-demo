"""Clients for Curve's two public APIs.

  * `https://api.curve.finance/v1`     -- pools, TVL, gauges, CRV APR
  * `https://prices.curve.finance/v1`  -- OHLC candles and snapshots

Neither needs a key. Both are Cloudflare-cached at `s-maxage=300`, so
`CurveApi` caches for the same 5 minutes rather than re-fetching bytes the
edge would only serve from cache anyway.

See docs/curve-api.md for the endpoint survey this is built on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .http import ApiError, build_url, get_json
from .models import Pool, attach_volumes

API_BASE = "https://api.curve.finance/v1"
PRICES_BASE = "https://prices.curve.finance/v1"

#: Match the CDN's shared cache window; see the `s-maxage=300` header.
CACHE_TTL = 300.0


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
    """Reads Curve's APIs, with a small time-based cache.

    One instance per app. Every method raises `ApiError` and nothing else,
    so a UI can wrap any call in a single `except`.
    """

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
        """Drop everything cached. Used by an explicit refresh."""
        self._cache.clear()

    # -- main API ---------------------------------------------------------

    async def _api(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET from the main API and unwrap its `{success, data}` envelope."""
        payload = await get_json(build_url(API_BASE, path, params))
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected response shape from {path}")
        if not payload.get("success", False):
            raise ApiError(f"Curve API reported a failure for {path}")
        data = payload.get("data")
        if data is None:
            raise ApiError(f"No data in response from {path}")
        return data

    async def platforms(self) -> dict[str, list[str]]:
        """Chain -> the registries deployed on it.

        Worth reading at startup rather than hardcoding: which registries
        exist per chain changes as Curve deploys new factories.
        """
        cached = self._cached("platforms")
        if cached is not None:
            return cached
        data = await self._api("/getPlatforms")
        platforms = data.get("platforms") or {}
        return self._store("platforms", {k: list(v) for k, v in platforms.items()})

    async def pools(self, chain: str = "ethereum") -> list[Pool]:
        """Every pool on a chain with at least $10k TVL, volume attached.

        Two requests, joined by address: `getPools/big` has no volume or
        base APY in it and `getVolumes` has no pool metadata. `big` rather
        than `all` because `all` is ~6x the bytes and the extra pools are
        all dead.
        """
        key = f"pools:{chain}"
        cached = self._cached(key)
        if cached is not None:
            return cached

        pool_data = await self._api(f"/getPools/big/{chain}")
        raw_pools = pool_data.get("poolData") or []
        pools = [Pool.from_api(raw, chain) for raw in raw_pools]
        pools = [p for p in pools if not p.is_broken and p.address]

        # A missing volume feed should degrade to "no volume shown", not to
        # an empty pool list -- the metadata half is still worth rendering.
        try:
            volume_data = await self._api(f"/getVolumes/{chain}")
            attach_volumes(pools, volume_data.get("pools") or [])
        except ApiError:
            pass

        return self._store(key, pools)

    async def chain_totals(self, chain: str = "ethereum") -> dict[str, float]:
        """Chain-wide headline numbers for the list view's header."""
        key = f"totals:{chain}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            data = await self._api(f"/getVolumes/{chain}")
            totals = data.get("totalVolumes") or {}
            return self._store(
                key,
                {
                    "volume": float(totals.get("totalVolume") or 0.0),
                    "crypto_share": float(totals.get("cryptoVolumeSharePcent") or 0.0),
                },
            )
        except ApiError:
            return {"volume": 0.0, "crypto_share": 0.0}

    # -- prices API -------------------------------------------------------
    #
    # Not enveloped like the main API: these return bare objects with a
    # `data` array.

    async def _prices(self, path: str, params: dict[str, Any] | None = None) -> Any:
        payload = await get_json(build_url(PRICES_BASE, path, params))
        if not isinstance(payload, dict):
            raise ApiError(f"Unexpected response shape from {path}")
        return payload

    async def lp_candles(
        self,
        chain: str,
        pool: str,
        *,
        days: int = 90,
        agg_number: int = 1,
        agg_units: str = "day",
        now: int | None = None,
    ) -> list[Candle]:
        """Candles for the pool's LP token price.

        This is the default series Curve's own pool page charts.
        """
        end = int(now if now is not None else time.time())
        params = {
            "start": end - days * 86400,
            "end": end,
            "agg_number": agg_number,
            "agg_units": agg_units,
        }
        key = f"lp_ohlc:{chain}:{pool}:{days}:{agg_number}{agg_units}"
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._prices(f"/lp_ohlc/{chain}/{pool}", params)
        candles = [Candle.from_api(c) for c in payload.get("data") or []]
        return self._store(key, candles)

    async def pair_candles(
        self,
        chain: str,
        pool: str,
        main_token: str,
        reference_token: str,
        *,
        days: int = 90,
        agg_number: int = 1,
        agg_units: str = "day",
        now: int | None = None,
    ) -> list[Candle]:
        """Candles for one coin priced in another, within a single pool.

        `main_token`/`reference_token` are coin addresses, not the pool's --
        the pool address only says which market to read them from.
        """
        end = int(now if now is not None else time.time())
        params = {
            "main_token": main_token,
            "reference_token": reference_token,
            "start": end - days * 86400,
            "end": end,
            "agg_number": agg_number,
            "agg_units": agg_units,
        }
        key = (
            f"ohlc:{chain}:{pool}:{main_token}:{reference_token}"
            f":{days}:{agg_number}{agg_units}"
        )
        cached = self._cached(key)
        if cached is not None:
            return cached
        payload = await self._prices(f"/ohlc/{chain}/{pool}", params)
        candles = [Candle.from_api(c) for c in payload.get("data") or []]
        return self._store(key, candles)
