"""Curve, as data and calldata -- with no Flet anywhere in it.

The split mirrors the one in `wallet/`: everything that can be reasoned
about without a running UI lives here, so it can be tested directly.

    api.py     reads Curve's APIs (v2 pools, v1 charts) -> Pool, Candle
    models.py  the domain objects and parsing
    sort.py    the pool list's ordering rules
    format.py  numbers -> the strings a table shows
    abi.py     calldata for the pool contracts
    pool.py    those calls, bound to a wallet
    http.py    the browser/desktop fetch seam
"""

from __future__ import annotations

from .api import Candle, CurveApi, PoolFeed
from .http import ApiError
from .models import Coin, Incentive, Pool
from .pool import PoolContract, PoolCallFailed
from .sort import DEFAULT_SORT, SORTS, search_pools, sort_pools

__all__ = [
    "CurveApi",
    "PoolFeed",
    "Candle",
    "ApiError",
    "Pool",
    "Coin",
    "Incentive",
    "PoolContract",
    "PoolCallFailed",
    "SORTS",
    "DEFAULT_SORT",
    "sort_pools",
    "search_pools",
]
