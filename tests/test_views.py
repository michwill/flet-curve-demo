"""Building every view off-screen, with no app and no display.

These are cheap and they earn their place: Flet validates control
arguments in `__init__`, so simply constructing the tree catches the whole
class of "wrong keyword for this Flet version" bug. Two real ones were
found this way rather than in the browser:

  * `PoolDetailView` assigned `self.page`, but `ft.Column` already defines
    `page` as a read-only property;
  * `ft.Tab` took a `content=` argument in older Flet and does not in 0.86,
    where the bodies moved to `TabBarView`.

Both only surfaced on the *second* click in a published build. A constructor
test finds them in 0.2s.

Note this is not Flet's own integration testing -- see README for that. It
needs no Flutter SDK because it never renders anything; it only builds the
Python-side control tree.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import flet as ft
import pytest

from curve.models import Pool
from ui.actions import DepositTab, StakeTab, SwapTab, WithdrawTab
from ui.candles import CandleChart
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView, PoolRow


class FakeFeed:
    """Stands in for `curve.api.PoolFeed`, without the network.

    Mirrors the real cursor's contract closely enough to exercise the view:
    a page at a time, a total, an exhausted flag, and a `reset` that drops
    everything loaded so far.
    """

    def __init__(self, pools, page_size: int = 50) -> None:
        self._all = pools
        self._page_size = page_size
        self.pools: list = []
        self.total: int | None = None
        self.loading = False
        self.error = ""
        self.sort_by = "volume"
        self.search = ""
        self.resets = 0

    @property
    def exhausted(self) -> bool:
        return self.total is not None and len(self.pools) >= self.total

    @property
    def loaded(self) -> int:
        return len(self.pools)

    def reset(self, *, sort_by=None, direction=None, search=None) -> None:
        if sort_by is not None:
            self.sort_by = sort_by
        if search is not None:
            self.search = search
        self.pools = []
        self.total = None
        self.resets += 1

    async def load_more(self) -> list:
        if self.exhausted:
            return []
        start = len(self.pools)
        page = self._all[start : start + self._page_size]
        self.total = len(self._all)
        self.pools.extend(page)
        return page


class StubPage:
    """Stands in for `ft.Page`. Records instead of rendering."""

    def __init__(self) -> None:
        self.updates = 0
        self.tasks: list = []

    def update(self) -> None:
        self.updates += 1

    def run_task(self, handler, *args):
        """Record the call, and actually run it when there is a loop.

        Sync tests have no running loop, so the coroutine is never even
        created there -- constructing one only to drop it produces a
        "never awaited" warning.
        """
        self.tasks.append(handler)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        return asyncio.ensure_future(handler(*args))


def make_pool(n_coins: int = 2, *, registry: str = "crvusd", gauge: str = "0xg") -> Pool:
    return Pool.from_v2(
        {
            "address": "0x" + "1" * 40,
            "name": "Curve.fi Test",
            "pool_type": registry,
            "gauges": [{"address": gauge, "is_killed": False}] if gauge else [],
            "crv_apr": 2.93,
            "crv_apr_boosted": 7.32,
            "extra_rewards_apr": [{"symbol": "OP", "apr": 1.2, "address": "0x" + "2" * 40}],
            "tvl_usd": 47_490_000.0,
            "coins": [
                {
                    "pool_index": i,
                    "symbol": f"C{i}",
                    "address": "0x" + f"{i:02x}" * 20,
                    "decimals": 18,
                    "usd_price": 1.0,
                }
                for i in range(n_coins)
            ],
        }
    )


# -- list ------------------------------------------------------------------


def test_pool_list_builds_and_attaches_a_feed() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    assert view.feed is None
    view.attach(FakeFeed([make_pool(), make_pool(3)]))
    assert view.rows.controls == []  # nothing until a page is pulled


async def test_pool_list_appends_pages_as_they_load() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    feed = FakeFeed([make_pool() for _ in range(7)], page_size=3)
    view.attach(feed)

    await view.load_more()
    assert len(view.rows.controls) == 3
    assert "3 of 7 pools" in view.count_label.value

    await view.load_more()
    await view.load_more()
    assert len(view.rows.controls) == 7
    assert view.count_label.value == "7 pools"

    # Exhausted: further calls are harmless no-ops.
    await view.load_more()
    assert len(view.rows.controls) == 7


async def test_scroll_near_the_end_is_what_triggers_a_page() -> None:
    """The whole point of paging: rows arrive because the list scrolled."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool() for _ in range(6)], page_size=3))
    await view.load_more()
    assert len(view.rows.controls) == 3

    far = SimpleNamespace(pixels=0.0, max_scroll_extent=99_999.0)
    view._scrolled(far)
    assert len(view.rows.controls) == 3  # nowhere near the end; no fetch

    near = SimpleNamespace(pixels=99_000.0, max_scroll_extent=99_100.0)
    view._scrolled(near)
    await asyncio.sleep(0)
    assert len(view.rows.controls) == 6


def test_sorting_resets_the_feed_rather_than_reordering_in_place() -> None:
    """A client cannot sort what it has not fully loaded, so the server does."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    feed = FakeFeed([make_pool() for _ in range(4)], page_size=2)
    view.attach(feed)
    view.rows.controls = [object(), object()]

    view._sort_by("tvl")
    assert feed.sort_by == "tvl"       # mapped to the v2 field name
    assert feed.resets == 1
    assert view.rows.controls == []    # cleared, awaiting page 1 of the new order


def test_sorting_by_the_active_column_is_a_no_op() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    feed = FakeFeed([make_pool()])
    view.attach(feed)
    view._sort_by("volume")  # already the default
    assert feed.resets == 0


# -- detail ----------------------------------------------------------------


@pytest.mark.parametrize("n_coins", [2, 3, 4])
def test_pool_detail_builds_for_any_coin_count(n_coins: int) -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(n_coins), get_contract=lambda: None,
        on_back=lambda: None,
    )
    assert isinstance(view, ft.Column)
    # LP token, plus every ordered pair of coins
    assert len(view.series.options) == 1 + n_coins * (n_coins - 1)


def test_pool_detail_builds_without_a_gauge() -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(gauge=""), get_contract=lambda: None,
        on_back=lambda: None,
    )
    assert view is not None


def test_tabs_length_matches_the_number_of_panels() -> None:
    """`ft.Tabs.length` must match TabBar.tabs and TabBarView.controls."""
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(), get_contract=lambda: None, on_back=lambda: None
    )
    assert len(view.tabs) == 4


# -- action tabs -----------------------------------------------------------


@pytest.mark.parametrize("tab_class", [DepositTab, WithdrawTab, SwapTab, StakeTab])
def test_action_tabs_mount_without_a_wallet(tab_class) -> None:
    async def noop() -> None:
        return None

    tab = tab_class(StubPage(), make_pool(3), lambda: None, noop)
    assert isinstance(tab.mount(), ft.Column)
    # Nothing is submittable until a wallet is connected.
    assert tab.submit_button.disabled


def test_stake_tab_says_so_when_there_is_no_gauge() -> None:
    async def noop() -> None:
        return None

    tab = StakeTab(StubPage(), make_pool(gauge=""), lambda: None, noop)
    tab.mount()
    text = " ".join(
        c.value for c in tab.control.controls if isinstance(c, ft.Text) and c.value
    )
    assert "no gauge" in text


def test_slippage_parsing_falls_back_on_nonsense() -> None:
    async def noop() -> None:
        return None

    tab = DepositTab(StubPage(), make_pool(), lambda: None, noop)
    tab.slippage.value = "1.5"
    assert tab.slippage_pct() == 1.5
    for bad in ("", "abc", "-1", "150"):
        tab.slippage.value = bad
        assert tab.slippage_pct() == 0.5


def test_deposit_parses_per_coin_decimals() -> None:
    async def noop() -> None:
        return None

    pool = make_pool(2)
    pool.coins[1].decimals = 6
    tab = DepositTab(StubPage(), pool, lambda: None, noop)
    tab.mount()
    tab.fields[0].value = "1"
    tab.fields[1].value = "1"
    assert tab._amounts() == [10**18, 10**6]


def test_deposit_ignores_unparseable_input_rather_than_raising() -> None:
    async def noop() -> None:
        return None

    tab = DepositTab(StubPage(), make_pool(2), lambda: None, noop)
    tab.mount()
    tab.fields[0].value = "not a number"
    assert tab._amounts() == [0, 0]


# -- chart -----------------------------------------------------------------


def test_candle_chart_builds_and_accepts_an_empty_series() -> None:
    chart = CandleChart()
    chart.set_candles([])
    assert chart._empty.visible
