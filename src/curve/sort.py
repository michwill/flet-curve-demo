"""The pool list's ordering options.

These are now *server-side*: the v2 API caps a page at 50 rows, so a client
cannot order a list it has not fully loaded, and every sort key here has to
be one the API understands (`PoolSortField` in its OpenAPI spec).

`local` is kept alongside each option purely so tests -- and any caller
holding a complete list -- can reproduce the server's ordering without a
network round trip. It is not what the list view uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import Pool


@dataclass(slots=True, frozen=True)
class SortOption:
    key: str
    label: str
    #: The v2 `sort_by` field this maps to.
    field: str
    #: Local equivalent, for tests and for sorting an in-memory list.
    local: Callable[[Pool], float]


#: Volume first: it is the default Curve's own UI opens on, and the closest
#: single proxy for "which pools are actually being used".
#:
#: "Incentives" maps to `aggregate_apr`, which is the API's combined figure
#: -- base + CRV + token rewards + merkle. It is the only server-side field
#: that accounts for reward tokens at all; there is no rewards-without-base
#: equivalent. In practice the difference is immaterial, since base APR is
#: low single digits where incentive APRs run to hundreds of percent.
SORTS: tuple[SortOption, ...] = (
    SortOption("volume", "Volume", "volume", lambda p: p.volume_24h),
    SortOption("tvl", "TVL", "tvl", lambda p: p.tvl),
    SortOption(
        "incentives",
        "Incentives",
        "aggregate_apr",
        lambda p: p.base_apr + p.incentives_apr,
    ),
    SortOption("base", "Base APY", "base_daily_apr", lambda p: p.base_apr),
)

DEFAULT_SORT = "volume"

_BY_KEY = {option.key: option for option in SORTS}


def get_sort(key: str) -> SortOption:
    """Look up a sort by key, falling back to the default."""
    return _BY_KEY.get(key, _BY_KEY[DEFAULT_SORT])


def sort_field(key: str) -> str:
    """The v2 `sort_by` value for a UI sort key."""
    return get_sort(key).field


def sort_pools(pools: list[Pool], key: str = DEFAULT_SORT) -> list[Pool]:
    """Order an in-memory list the way the server would, descending.

    Ties break on TVL then address so the order is total: without that a
    re-sort can shuffle equal rows -- very common, since hundreds of pools
    share a volume of exactly zero -- which reads as a flicker.
    """
    option = get_sort(key)
    return sorted(
        pools,
        key=lambda p: (-option.local(p), -p.tvl, p.address.lower()),
    )


def search_pools(pools: list[Pool], query: str) -> list[Pool]:
    """Filter an in-memory list by name, symbol, coin or address.

    The list view sends the query to the server instead (`search_string`),
    which searches the whole chain rather than the pages already loaded.
    This stays for tests and for filtering a list already in hand.
    """
    text = (query or "").strip().lower()
    if not text:
        return pools

    def matches(pool: Pool) -> bool:
        if text in pool.name.lower() or text in pool.address.lower():
            return True
        return any(
            text in coin.symbol.lower() or text in coin.address.lower()
            for coin in pool.coins
        )

    return [p for p in pools if matches(p)]
