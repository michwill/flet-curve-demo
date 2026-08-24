"""The pool list's ordering options."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
SORTS: tuple[SortOption, ...] = (
    SortOption("volume", "Volume", "volume", lambda p: p.volume_24h),
    SortOption("tvl", "TVL", "tvl", lambda p: p.tvl),
    # Both of these name the field the *column* draws, which is not the field
    # that reads most naturally.  `aggregate_apr` counts the base APY as well,
    # so a pool paying 221% base and no incentives at all led the Incentives
    # column; `base_daily_apr` is a different window from the weekly figure
    # beside it, and put 15.49% below 2.35%.  Measured over a page of 25:
    # 7 and 13 neighbours out of order, against 2 and 0 for these.
    SortOption("incentives", "Incentives", "rewards_apr",
               lambda p: p.incentives_apr),
    SortOption("base", "Base APY", "base_weekly_apr", lambda p: p.base_apr),
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
    """Order an in-memory list the way the server would, descending."""
    option = get_sort(key)
    return sorted(
        pools,
        key=lambda p: (-option.local(p), -p.tvl, p.address.lower()),
    )


def search_pools(pools: list[Pool], query: str) -> list[Pool]:
    """Filter an in-memory list by name, symbol, coin or address."""
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
