"""Ordering and filtering the pool list.

Split out of the UI so the ranking rules are testable on plain data, and
so "sorted by incentives" has exactly one definition in the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import Pool


@dataclass(slots=True, frozen=True)
class SortOption:
    key: str
    label: str
    #: Extracts the number to order by. Always sorted descending.
    value: Callable[[Pool], float]


#: Volume first: it is the default the real Curve UI opens on, and it is the
#: closest thing to "which pools are actually being used".
SORTS: tuple[SortOption, ...] = (
    SortOption("volume", "Volume", lambda p: p.volume_24h),
    SortOption("tvl", "TVL", lambda p: p.tvl),
    SortOption("incentives", "Incentives", lambda p: p.incentives_apr),
    SortOption("base", "Base APY", lambda p: p.base_apr),
)

DEFAULT_SORT = "volume"

_BY_KEY = {option.key: option for option in SORTS}


def get_sort(key: str) -> SortOption:
    """Look up a sort by key, falling back to the default."""
    return _BY_KEY.get(key, _BY_KEY[DEFAULT_SORT])


def sort_pools(pools: list[Pool], key: str = DEFAULT_SORT) -> list[Pool]:
    """Order pools by one of `SORTS`, descending.

    Ties break on TVL then address so the order is total: without that a
    re-sort can shuffle equal rows (very common -- hundreds of pools share
    a volume of exactly 0), which reads as a flicker on every refresh.
    """
    option = get_sort(key)
    return sorted(
        pools,
        key=lambda p: (-option.value(p), -p.tvl, p.address.lower()),
    )


def search_pools(pools: list[Pool], query: str) -> list[Pool]:
    """Filter by pool name, symbol, coin symbol, or address.

    Pasting a pool or token address is the fast path people actually use,
    so addresses match on any substring rather than requiring the full 42
    characters.
    """
    text = (query or "").strip().lower()
    if not text:
        return pools

    def matches(pool: Pool) -> bool:
        if text in pool.name.lower() or text in pool.symbol.lower():
            return True
        if text in pool.address.lower():
            return True
        return any(
            text in coin.symbol.lower() or text in coin.address.lower()
            for coin in pool.coins
        )

    return [p for p in pools if matches(p)]
