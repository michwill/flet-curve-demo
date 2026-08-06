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
from ui.responsive import layout_for


class FakeFeed:
    """Stands in for `curve.api.PoolFeed`, without the network.

    Mirrors the real cursor's contract closely enough to exercise the view:
    a page at a time, a total, an exhausted flag, and a `reset` that drops
    everything loaded so far.
    """

    def __init__(self, pools, page_size: int = 50, *, lite: bool = False) -> None:
        self._all = pools
        self._page_size = page_size
        self.pools: list = []
        self.total: int | None = None
        self.loading = False
        self.error = ""
        #: As on the real feed: a Curve Lite chain, which has no volume or
        #: base APR for the view to show.
        self.lite = lite
        self.sort_by = "tvl" if lite else "volume"
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


# -- responsive ------------------------------------------------------------

PHONE, TABLET, LAPTOP = 390.0, 820.0, 1280.0


def test_the_list_swaps_headers_for_a_sort_dropdown_on_a_phone() -> None:
    """Cards have no columns to click, so sorting needs its own control."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.set_layout(layout_for(LAPTOP))
    assert view._header.visible and not view.sort_picker.visible

    view.set_layout(layout_for(PHONE))
    assert view.sort_picker.visible and not view._header.visible


def test_the_table_hides_its_least_decisive_column_on_a_tablet() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.set_layout(layout_for(TABLET))
    assert not view._sort_cells["base"].visible
    assert view._sort_cells["volume"].visible


def test_rows_are_rebuilt_when_the_layout_changes() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool(), make_pool(3)]))
    import asyncio

    asyncio.run(view.load_more())
    wide_rows = list(view.rows.controls)
    view.set_layout(layout_for(PHONE))
    assert len(view.rows.controls) == len(wide_rows)
    assert view.rows.controls[0] is not wide_rows[0]  # rebuilt, not reused


def test_a_repeated_layout_is_a_no_op() -> None:
    """Resize fires constantly; rebuilding rows every time would thrash."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool()]))
    import asyncio

    asyncio.run(view.load_more())
    view.set_layout(layout_for(PHONE))
    first = view.rows.controls[0]
    view.set_layout(layout_for(PHONE + 5))  # same layout
    assert view.rows.controls[0] is first


def test_pool_rows_build_in_every_layout() -> None:
    for width in (PHONE, TABLET, LAPTOP):
        row = PoolRow(make_pool(3), lambda _p: None, 0, layout_for(width))
        assert row is not None


def test_the_pool_page_stacks_on_a_phone_and_splits_on_a_laptop() -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(), get_contract=lambda: None,
        on_back=lambda: None,
    )
    view.set_layout(layout_for(LAPTOP))
    assert isinstance(view._body.content, ft.Row)

    view.set_layout(layout_for(PHONE))
    assert isinstance(view._body.content, ft.Column)


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


# -- Curve Lite ------------------------------------------------------------
#
# These chains have no volume and no base APR -- nothing indexes their
# trades -- so those two columns are not empty, they are absent. See
# `curve.lite` for the API and `test_lite.py` for the parsing.


def make_lite_pool(tvl: float = 2_289_892.0) -> Pool:
    return Pool.from_lite(
        {
            "address": "0x" + "ab" * 20,
            "chain_id": 42793,
            "name": "mBASIS/USDC",
            "registry_id": "factory_stable_ng",
            "tvl": tvl,
            "lp_token_address": "0x" + "ab" * 20,
            "coins": [
                {
                    "address": "0x" + f"{i:02x}" * 20,
                    "symbol": f"C{i}",
                    "decimals": "18",
                    "usd_price": 1.0,
                    "pool_balance": "1000000000000000000",
                }
                for i in range(2)
            ],
        },
        "etherlink",
    )


def test_a_lite_row_drops_the_columns_that_measure_trading() -> None:
    from ui.pool_list import COLUMN_WIDTH, visible_columns

    wide = layout_for(2000.0)
    assert set(visible_columns(wide.columns, False)) == set(wide.columns)
    lite = visible_columns(wide.columns, True)
    assert "volume" not in lite and "base" not in lite
    assert "tvl" in lite and "incentives" in lite
    # Whatever survives still has a width to lay out with.
    assert all(key in COLUMN_WIDTH for key in lite)


def test_a_lite_row_is_narrower_than_a_full_one() -> None:
    """Same layout, fewer cells: the row builder reads the pool itself, so
    a Lite pool and a full one can sit in the same list."""
    layout = layout_for(2000.0)
    full = PoolRow(make_pool(), on_open=lambda _p: None, layout=layout)
    lite = PoolRow(make_lite_pool(), on_open=lambda _p: None, layout=layout)
    assert len(lite.content.controls) == len(full.content.controls) - 2


def test_a_lite_header_hides_those_sorts() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_lite_pool()], lite=True))
    hidden = [key for key, cell in view._sort_cells.items() if not cell.visible]
    assert "volume" in hidden and "base" in hidden
    # And the phone's dropdown offers the same set the header does.
    assert {o.key for o in view.sort_picker.options} == {"tvl", "incentives"}


def test_a_lite_list_opens_on_tvl() -> None:
    """Sorting by a volume that is unknown everywhere orders the page
    arbitrarily, so the feed opens on TVL and the header agrees."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_lite_pool()], lite=True))
    assert view._sort == "tvl"
    assert view.sort_picker.value == "tvl"


def test_a_full_list_still_opens_on_volume() -> None:
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool()]))
    assert view._sort == "volume"
    assert {o.key for o in view.sort_picker.options} == {
        "volume", "tvl", "incentives", "base"
    }


def test_a_lite_card_leads_with_tvl() -> None:
    """On a phone the headline number is whichever one exists."""
    card = PoolRow(make_lite_pool(), on_open=lambda _p: None, layout=layout_for(400.0))
    labels = [c.value for c in _texts(card) if isinstance(c.value, str)]
    assert "TVL" in labels
    assert "base" not in [label.lower() for label in labels]


def _contains(control, target) -> bool:
    """Is `target` anywhere in this control's tree? The chart's picker sits
    inside a Row, not directly under the column."""
    if control is target:
        return True
    for attr in ("controls", "content"):
        child = getattr(control, attr, None)
        if isinstance(child, list):
            if any(_contains(item, target) for item in child):
                return True
        elif child is not None and _contains(child, target):
            return True
    return False


def _texts(control, found=None) -> list:
    found = [] if found is None else found
    if isinstance(control, ft.Text):
        found.append(control)
    for attr in ("controls", "content"):
        child = getattr(control, attr, None)
        if isinstance(child, list):
            for item in child:
                _texts(item, found)
        elif child is not None:
            _texts(child, found)
    return found


def test_a_lite_pool_page_shows_no_chart() -> None:
    """There is no OHLC endpoint for these chains at all, so the picker
    and the canvas are replaced by a line saying why."""
    view = PoolDetailView(
        StubPage(), api=None, pool=make_lite_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    assert not _contains(view._left, view.series)
    assert not _contains(view._left, view.chart)
    assert any(
        "No price history" in (text.value or "") for text in _texts(view._left)
    )


def test_a_lite_pool_page_reports_no_volume() -> None:
    """Same reason the column goes: "$0" reads as a quiet day rather than
    as a measurement nobody took."""
    lite = PoolDetailView(
        StubPage(), api=None, pool=make_lite_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    full = PoolDetailView(
        StubPage(), api=None, pool=make_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    labels = [text.value for text in _texts(lite.controls[0])]
    assert "TVL" in labels and "24h volume" not in labels
    assert "24h volume" in [text.value for text in _texts(full.controls[0])]


def test_a_full_pool_page_still_shows_one() -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    assert _contains(view._left, view.chart)
    assert _contains(view._left, view.series)
