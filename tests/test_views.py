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
    import asyncio

    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool(), make_pool(3)]))
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


# -- themes ----------------------------------------------------------------
#
# Three now: Material's light and dark, and Chad -- a hand-set palette from
# linux.org.ru, which differs in shape as well as colour because its panels
# carry a hard shadow. See `ui/theme.py`.


def test_the_three_themes_are_what_the_button_cycles() -> None:
    from ui import theme

    assert theme.NAMES == ("light", "dark", "chad")


def test_light_and_dark_are_generated_and_chad_is_not() -> None:
    """Material can produce a great many palettes from a seed; the Tango
    palette off a Russian web forum is not among them."""
    from ui import theme

    assert theme.material().color_scheme_seed is not None
    assert theme.material().color_scheme is None
    assert theme.chad().color_scheme_seed is None
    assert theme.chad().color_scheme is not None


def test_chad_is_the_palette_that_site_actually_serves() -> None:
    """Pinned to the values read off the live page, because the first
    version of this theme took them from a stylesheet the site does not
    use by default and every one of them was wrong."""
    from ui import theme

    assert theme.PAGE == "#D3D7CF"    # --main-background
    assert theme.PANEL == "#EEEEEC"   # --article-background
    assert theme.HOVER == "#AD7FA8"   # --table-hover-background: plum, not amber
    assert theme.RULE == "#BABDB6"    # --table-border-color
    assert theme.ACTIVE == "#C17D11"  # --icon-button-active-color
    assert theme.LABEL == "#E9B96E"   # --tagpage-group-label-background
    assert theme.BROWN == "#8F5902"   # --main-menu-color
    assert theme.LINK == "#204A87"    # --link-color


def test_chad_uses_the_stylesheets_own_colours() -> None:
    from ui import theme

    scheme = theme.chad().color_scheme
    assert scheme.primary == theme.ACTIVE          # --icon-button-active-color
    assert scheme.secondary == theme.BROWN         # --main-menu-color
    assert scheme.surface == theme.PANEL           # --article-background
    assert scheme.surface_container == theme.PAGE  # --main-background
    assert scheme.outline_variant == theme.RULE    # --table-border-color
    assert scheme.error == theme.DANGER            # --button-danger-background


def test_chad_is_pinned_to_light_mode() -> None:
    """It is a light theme with its own colours; leaving the mode on
    SYSTEM would let a dark desktop swap `dark_theme` in behind it."""
    import flet as ft

    from ui import theme

    assert theme.theme_for("chad")[1] == ft.ThemeMode.LIGHT
    assert theme.theme_for("dark")[1] == ft.ThemeMode.DARK
    assert theme.theme_for("light")[1] == ft.ThemeMode.LIGHT


def test_an_unknown_theme_name_lands_on_light() -> None:
    from ui import theme

    assert theme.theme_for("nonsense")[0].color_scheme is None


def test_the_shadow_is_hard_edged() -> None:
    """The point of it: Material's elevation is a blurred gradient, and
    this theme wants one colour and one edge."""
    from ui import theme

    for shadow in (theme.PANEL_SHADOW, theme.INSET_SHADOW):
        assert shadow.blur_radius == 0
        assert shadow.spread_radius == 0
        assert shadow.offset.x > 0 and shadow.offset.y > 0


class ThemedPage(StubPage):
    def __init__(self, name: str = "light") -> None:
        super().__init__()
        from ui import theme

        self.theme, self.theme_mode = theme.theme_for(name)


def test_shadows_are_chads_alone() -> None:
    from ui import theme

    assert theme.panel_shadow(ThemedPage("chad")) is theme.PANEL_SHADOW
    assert theme.panel_shadow(ThemedPage("chad"), inset=True) is theme.INSET_SHADOW
    assert theme.panel_shadow(ThemedPage("light")) is None
    assert theme.panel_shadow(ThemedPage("dark")) is None


def test_a_page_that_cannot_answer_is_not_chad() -> None:
    """A stub, or a page whose theme has not been set yet. The useful
    default is no shadow rather than an exception."""
    from ui import theme

    assert theme.is_chad(StubPage()) is False
    assert theme.panel_shadow(StubPage()) is None


def test_an_action_panel_takes_the_shadow_under_chad() -> None:
    from ui import theme
    from ui.actions import DepositTab

    chad = DepositTab(ThemedPage("chad"), make_pool(), lambda: None, None)
    plain = DepositTab(ThemedPage("light"), make_pool(), lambda: None, None)

    assert chad.status_panel.shadow is theme.INSET_SHADOW
    assert plain.status_panel.shadow is None


def test_a_row_goes_plum_under_the_pointer_in_chad() -> None:
    """The one colour that makes the theme recognisable, and the one an
    ink overlay cannot produce on its own -- Material's default hover is a
    translucent tint of the surface."""
    from ui import theme

    view = PoolListView(ThemedPage("chad"), on_open=lambda _p: None)

    assert view._rows_box.theme.hover_color == theme.HOVER


def test_elsewhere_the_row_leaves_the_hover_to_material() -> None:
    view = PoolListView(ThemedPage("light"), on_open=lambda _p: None)
    assert view._rows_box.theme is None


def test_nothing_is_assigned_to_a_row_after_it_is_built() -> None:
    """Rows carry `key="pool-row-N"`, and Flet freezes a keyed control
    once a rebuild has matched it to its predecessor by key: assigning to
    any property then raises "Frozen controls cannot be updated". An
    `on_hover` that painted `bgcolor` did exactly that -- fine until the
    first theme change, then an unhandled error on the next hover."""
    row = PoolRow(make_pool(), lambda _p: None, 0)

    assert row.on_hover is None
    assert row.ink is True

    row._frozen = True  # what Flet does to a keyed control it re-diffs
    with pytest.raises(RuntimeError, match="Frozen"):
        row.bgcolor = "#FF0000"


def test_a_theme_change_leaves_the_rows_where_they_are() -> None:
    """Which is the fix for the frozen-control bug: re-making a keyed row
    is what freezes it."""
    from ui import theme

    view = PoolListView(ThemedPage("light"), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool(), make_pool()]))
    view.rows.controls = [PoolRow(make_pool(), lambda _p: None, i) for i in range(2)]
    before = list(view.rows.controls)

    view._page.theme, view._page.theme_mode = theme.theme_for("chad")
    view.rebuild()

    assert all(a is b for a, b in zip(view.rows.controls, before, strict=True))
    assert view._rows_box.theme.hover_color == theme.HOVER


def test_the_column_headings_get_a_band_under_chad() -> None:
    """`thead` takes the table's border colour on that site: a grey strip
    above the rows. A Material table has no band at all."""
    from ui import theme

    chad = PoolListView(ThemedPage("chad"), on_open=lambda _p: None)
    plain = PoolListView(ThemedPage("light"), on_open=lambda _p: None)

    assert chad._header.bgcolor == theme.RULE
    assert plain._header.bgcolor is None


def test_the_outline_is_chads_alone() -> None:
    """Chad is a theme of bordered boxes, and its shadows need an edge to
    come from. Light and dark separate by tone, and looked better without
    an extra line."""
    from ui import theme

    chad = PoolListView(ThemedPage("chad"), on_open=lambda _p: None)
    plain = PoolListView(ThemedPage("light"), on_open=lambda _p: None)

    assert chad._table.border is not None
    assert plain._table.border is None
    # ...and it comes and goes with the theme, on a container that
    # outlives the change.
    plain._page.theme, plain._page.theme_mode = theme.theme_for("chad")
    plain.rebuild()
    assert plain._table.border is not None


def test_the_top_bar_casts_straight_down() -> None:
    """A bar that reaches both window edges has no side to cast from, so
    this one is the only shadow here with no sideways offset."""
    from ui import theme

    assert theme.bar_shadow(ThemedPage("chad")) is theme.BAR_SHADOW
    assert theme.bar_shadow(ThemedPage("light")) is None
    assert theme.BAR_SHADOW.offset.x == 0
    assert theme.BAR_SHADOW.offset.y > 0
    assert theme.BAR_SHADOW.blur_radius == 0


def test_a_pool_page_takes_the_shadow_under_chad() -> None:
    from ui import theme

    chad = PoolDetailView(
        ThemedPage("chad"), api=None, pool=make_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    plain = PoolDetailView(
        ThemedPage("light"), api=None, pool=make_pool(),
        get_contract=lambda: None, on_back=lambda: None,
    )
    # `_right` is the slot; the bordered box inside it is what casts.
    assert chad._right.content.shadow is theme.PANEL_SHADOW
    assert plain._right.content.shadow is None


# -- remembering the theme -------------------------------------------------
#
# Both halves of the storage API are coroutines, and calling one without
# awaiting it fails *silently* -- the write never happens and the read
# returns a coroutine object that no `isinstance` will match. So these
# tests use a fake that is async in the same way.


class FakePreferences:
    def __init__(self, stored: dict | None = None) -> None:
        self.stored = dict(stored or {})

    async def get(self, key):
        return self.stored.get(key)

    async def set(self, key, value) -> bool:
        self.stored[key] = value
        return True


class StoringPage(ThemedPage):
    def __init__(self, name: str = "light") -> None:
        super().__init__(name)
        self.bgcolor = None


def themed_app(page, stored: dict | None = None):
    """`CurveApp` with its constructor skipped -- only the theme parts."""
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = page
    app.storage = FakePreferences(stored)
    app._sync_theme_button = lambda update=False: None  # type: ignore[method-assign]
    app._rebuild_view = lambda: None                    # type: ignore[method-assign]
    return app


def button_app(page):
    """`themed_app` with the real `_sync_theme_button`, and a button for it
    to draw into."""
    app = themed_app(page)
    del app._sync_theme_button  # the stub; these tests are about the real one
    app.theme_button = ft.Container()
    return app


@pytest.mark.parametrize(
    "name,expected",
    [("light", ft.Icons.LIGHT_MODE), ("dark", ft.Icons.DARK_MODE)],
)
def test_the_button_shows_the_theme_you_are_in(name, expected) -> None:
    """Not the one a click would get you, which is what it did first: a
    moon on a plainly light screen says the opposite of what is true."""
    page = StoringPage(name)
    page.platform_brightness = ft.Brightness.LIGHT
    app = button_app(page)

    app._sync_theme_button()

    assert app.theme_button.content.icon == expected


def test_chad_gets_the_chad() -> None:
    page = StoringPage("chad")
    page.platform_brightness = ft.Brightness.LIGHT
    app = button_app(page)

    app._sync_theme_button()

    assert isinstance(app.theme_button.content, ft.Image)
    assert app.theme_button.content.src.endswith("chad.png")


def test_the_tooltip_carries_the_destination() -> None:
    """Which is where there is room to say it in words."""
    page = StoringPage("dark")
    page.platform_brightness = ft.Brightness.LIGHT
    app = button_app(page)

    app._sync_theme_button()

    assert app.theme_button.tooltip == "Dark theme — click for chad"


def test_the_cycle_is_light_dark_chad_and_round() -> None:
    page = StoringPage("light")
    page.platform_brightness = ft.Brightness.LIGHT
    app = button_app(page)
    seen = []

    for _ in range(4):
        app._toggle_theme(None)
        seen.append(app._theme_name())

    assert seen == ["dark", "chad", "light", "dark"]


async def test_choosing_a_theme_writes_it_down() -> None:
    import main as app_module

    page = StoringPage()
    app = themed_app(page)

    app._set_theme("chad")

    # Scheduled rather than awaited: the theme is already on screen.
    assert page.tasks == [app._remember_theme]
    await app._remember_theme("chad")
    assert app.storage.stored[app_module.THEME_KEY] == "chad"


async def test_the_remembered_theme_comes_back() -> None:
    import main as app_module

    page = StoringPage()
    app = themed_app(page, {app_module.THEME_KEY: "chad"})

    await app.restore_theme()

    from ui import theme

    assert theme.is_chad(page)
    assert page.bgcolor == theme.PAGE


async def test_restoring_does_not_write_back_what_it_just_read() -> None:
    """It would be harmless, but a write on every load is a write that can
    fail on every load."""
    import main as app_module

    page = StoringPage()
    app = themed_app(page, {app_module.THEME_KEY: "dark"})

    await app.restore_theme()

    assert page.tasks == []


async def test_nothing_remembered_leaves_the_theme_alone() -> None:
    page = StoringPage("light")
    was = page.theme
    await themed_app(page).restore_theme()
    assert page.theme is was


async def test_junk_in_storage_is_ignored() -> None:
    """Storage is shared with whatever else lives on this origin, and the
    value could be from an older version of this app."""
    import main as app_module

    for junk in ("solarized", "", 7, ["chad"]):
        page = StoringPage()
        was = page.theme
        await themed_app(page, {app_module.THEME_KEY: junk}).restore_theme()
        assert page.theme is was


async def test_storage_that_will_not_answer_is_not_fatal() -> None:
    """A private window, or a desktop with no writable state directory.
    The app should open in the default theme, not fail to open."""
    page = StoringPage()
    app = themed_app(page)

    async def broken(key):
        raise RuntimeError("no storage here")

    app.storage.get = broken
    was = page.theme

    await app.restore_theme()

    assert page.theme is was


# -- pool parameters -------------------------------------------------------


def detail_view(page=None, **kw):
    return PoolDetailView(
        page or StubPage(), api=None, pool=make_pool(),
        get_contract=lambda: None, on_back=lambda: None, **kw,
    )


def texts(control) -> list[str]:
    """Every string in a subtree, for asserting on what a panel says."""
    found: list[str] = []

    def walk(node, seen):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ft.Text) and node.value:
            found.append(node.value)
        if isinstance(node, ft.Control):
            for name in node.__dataclass_fields__:
                walk(getattr(node, name, None), seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, seen)

    walk(control, set())
    return found


def test_the_parameters_start_folded_away() -> None:
    """Reference material: wanted rarely, and precisely when wanted."""
    view = detail_view()
    assert isinstance(view._parameters_slot.content, ft.ExpansionTile)
    assert "Pool parameters" in texts(view._parameters_slot)


def test_the_registry_line_is_gone() -> None:
    """It spent two thirds of its width on the registry name and the word
    "plain", neither of which anybody can act on."""
    view = detail_view()
    assert not any("plain" in value for value in texts(view._yields_slot))


def test_a_wide_page_prints_the_whole_address() -> None:
    """An address you have to hover to read is no use for checking a
    contract against one you already have."""
    view = detail_view()
    view.set_layout(layout_for(LAPTOP))
    assert make_pool().address in texts(view._parameters_slot)


def test_a_phone_shortens_it() -> None:
    """42 characters wrap onto three lines and push the row off screen."""
    view = detail_view()
    view.set_layout(layout_for(PHONE))
    shown = texts(view._parameters_slot)
    assert make_pool().address not in shown
    assert any("…" in value for value in shown)


def test_the_gauge_is_listed_when_there_is_one() -> None:
    with_gauge = detail_view()
    assert "Gauge" in texts(with_gauge._parameters_slot)


def test_a_pool_with_no_gauge_lists_no_gauge_row() -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(gauge=""),
        get_contract=lambda: None, on_back=lambda: None,
    )
    assert "Gauge" not in texts(view._parameters_slot)


def test_each_address_links_to_the_chains_explorer() -> None:
    from curve import explorers
    from curve.models import Coin, Pool

    pool = Pool(
        address="0x" + "11" * 20, name="P", chain="ethereum", chain_id=1,
        registry="main", lp_token="0x" + "11" * 20,
        coins=[Coin("0x" + "22" * 20, "C", 18, index=0)],
    )
    view = PoolDetailView(
        StubPage(), api=None, pool=pool,
        get_contract=lambda: None, on_back=lambda: None,
    )
    links = _links(view._parameters_slot)

    assert links == [explorers.address_url(1, pool.address)]
    assert links[0].startswith("https://etherscan.io/address/0x")


def test_a_pool_on_a_chain_with_no_table_entry_still_links_somewhere() -> None:
    """blockscan searches across chains: a link that lands somewhere
    useful beats an address you cannot click."""
    from curve import explorers

    view = detail_view()          # the stub pool names no chain at all
    assert all(link.startswith(explorers.FALLBACK) for link in _links(view._parameters_slot))


def test_a_lite_chain_links_to_the_explorer_it_publishes() -> None:
    """Those are the chains a hardcoded table would be wrong about."""
    view = detail_view(explorer="https://monadscan.com/")
    links = _links(view._parameters_slot)
    assert links
    assert all(link.startswith("https://monadscan.com/address/") for link in links)


def _links(root) -> list[str]:
    return [
        control.url.url
        for control in _all_controls(root)
        if isinstance(control, ft.IconButton) and control.url is not None
    ]


def _all_controls(root):
    seen: set[int] = set()
    out: list = []

    def walk(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ft.Control):
            out.append(node)
            for name in node.__dataclass_fields__:
                walk(getattr(node, name, None))
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(root)
    return out


async def test_with_no_contract_the_addresses_stay_and_the_values_say_why() -> None:
    view = detail_view()
    await view.load_parameters()
    assert "Connect a wallet to read them." in texts(view._parameter_rows)


async def test_the_values_land_in_the_panel() -> None:
    class Contract:
        async def parameters(self):
            return {"A": 4_000, "fee": 1_500_000}

    contract = Contract()
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(),
        get_contract=lambda: contract,  # type: ignore[return-value]
        on_back=lambda: None,
    )
    await view.load_parameters()

    shown = texts(view._parameter_rows)
    assert "A" in shown and "4,000" in shown
    assert "0.0150%" in shown


async def test_a_chain_that_cannot_be_read_says_so_rather_than_showing_nothing() -> None:
    from wallet.base import WalletError

    class Contract:
        async def parameters(self):
            raise WalletError("No public node is known for this network.")

    contract = Contract()
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(),
        get_contract=lambda: contract,  # type: ignore[return-value]
        on_back=lambda: None,
    )
    await view.load_parameters()

    assert any("No public node" in value for value in texts(view._parameter_rows))


def test_the_action_panel_is_sized_to_what_the_pool_puts_in_it() -> None:
    """`TabBarView` cannot size to its content, so the panel has to be
    told -- and telling it one number for every pool is what left a
    Deposit button floating in a few hundred pixels of empty card."""
    from ui.pool_detail import ACTIONS_MAX, ACTIONS_MIN, ACTIONS_ROW, actions_height

    two, three = actions_height(make_pool(2)), actions_height(make_pool(3))
    assert three - two == ACTIONS_ROW
    assert ACTIONS_MIN <= two <= ACTIONS_MAX


def test_a_metapool_gets_room_for_its_switch_and_its_underlying_rows() -> None:
    """The underlying route lists more rows than the pool has coins, and
    it is the one that opens by default."""
    from curve.models import Coin, Pool
    from ui.pool_detail import actions_height

    meta = Pool(
        address="0x" + "11" * 20, name="Meta", chain="ethereum", chain_id=1,
        registry="stableswapng", lp_token="0x" + "11" * 20, base_pool="0x" + "33" * 20,
        coins=[Coin("0x" + f"{i:02x}" * 20, f"C{i}", 18, index=i) for i in range(4)],
    )
    meta.onchain_coins = 2

    assert meta.has_underlying
    assert actions_height(meta) > actions_height(make_pool(2))


def test_the_panel_never_runs_off_a_laptop_screen() -> None:
    """Past the ceiling the tab body scrolls, which it is built to do."""
    from ui.pool_detail import ACTIONS_MAX, actions_height

    assert actions_height(make_pool(8)) == ACTIONS_MAX


# -- the header nav --------------------------------------------------------


def nav_app(page=None, on: str = "pools"):
    """`CurveApp` with only the header parts, as `themed_app` does."""
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = page or StubPage()
    app._page_name = on
    app.nav = ft.Container(width=0)                # closed, as the app starts
    app.menu = ft.PopupMenuButton(visible=False)   # wide, as the app starts
    app.totals = ft.Text("")
    return app


def test_the_current_page_is_marked_and_the_other_is_not() -> None:
    """Colour alone is a weak signal, so the current page is underlined
    as well -- and it is the *page*, not the hover, that decides."""
    app = nav_app(on="portfolio")
    app._sync_nav()
    links = {link.content.value: link for link in app.nav.content.controls}

    assert links["Portfolio"].border is not None
    assert links["Pools"].border is None
    assert links["Portfolio"].content.color != links["Pools"].content.color


def test_the_links_are_as_big_as_a_pool_name_and_bold() -> None:
    from ui.typography import ROW_TITLE

    app = nav_app()
    app._sync_nav()
    for link in app.nav.content.controls:
        assert link.content.size == ROW_TITLE
        assert link.content.weight == ft.FontWeight.BOLD


def test_the_links_are_containers_not_text_buttons() -> None:
    """A `TextButton` in this app hovers correctly and never fires its
    handler in the published web build -- the sortable column headings
    are Containers for the same reason."""
    app = nav_app()
    app._sync_nav()
    for link in app.nav.content.controls:
        assert isinstance(link, ft.Container)
        assert link.on_click is not None


def test_hovering_opens_the_nav_and_fades_the_totals() -> None:
    from main import NAV_WIDTH

    app = nav_app()
    app._brand_hovered(SimpleNamespace(data=True))
    assert app.nav.width == NAV_WIDTH
    assert app.totals.opacity == 0.0

    app._brand_hovered(SimpleNamespace(data=False))
    assert app.nav.width == 0
    assert app.totals.opacity == 1.0


def test_a_narrow_page_uses_the_menu_button_instead() -> None:
    """There is no room to slide anything open, so hovering does nothing
    and the mark is a menu button."""
    app = nav_app()
    app.menu.visible = True

    app._brand_hovered(SimpleNamespace(data=True))

    assert app.nav.width == 0
    assert app.totals.opacity == 1.0


# -- the portfolio page ----------------------------------------------------


def make_holding(**kw):
    from curve.portfolio import Holding

    base = {
        "address": "0x" + "11" * 20, "name": "pyUSD/crvUSD", "chain": "ethereum",
        "wallet": 3 * 10**18, "staked": 0, "tvl": 100.0, "supply": 10.0,
        "coins": (("0x" + "aa" * 20, "PYUSD"), ("0x" + "bb" * 20, "crvUSD")),
        "lp_token": "0x" + "11" * 20, "gauge": "",
    }
    base.update(kw)
    return Holding(**base)


def portfolio_view(page=None):
    from ui.portfolio import PortfolioView

    return PortfolioView(page or StubPage(), on_open=lambda _h: None)


def test_the_portfolio_draws_its_marks_the_way_the_pool_list_does() -> None:
    """Same helper, same size, same overlap -- they are the same kind of
    row in the same column, and the portfolio was drawing lettered discs
    because a holding carried symbols but no addresses.

    Asserted against `coin_stack` itself rather than against an image:
    with no compiled assets on the machine running this, every mark is
    the lettered fallback, which is exactly what the pool list draws
    there too."""
    from curve.models import Coin
    from ui.logos import coin_stack
    from ui.portfolio import LOGO_SIZE

    holding = make_holding()
    view = portfolio_view()
    view.show([holding])

    drawn = view.rows.controls[0].content.controls[0].controls[0]
    expected = coin_stack(
        [Coin(a, s, 18, index=n) for n, (a, s) in enumerate(holding.coins)],
        holding.chain,
        LOGO_SIZE,
    )
    assert drawn.width == expected.width
    assert drawn.height == expected.height == LOGO_SIZE


def test_the_heading_is_one_line() -> None:
    """The page name on the left, what it comes to on the right. The
    address is in the top bar already and is not repeated."""
    view = portfolio_view()
    view.show([make_holding()])
    heading = view.controls[0]

    assert isinstance(heading, ft.Row)
    assert [t for t in texts(heading) if t == "Portfolio"]
    assert any("Total value" in t for t in texts(heading))
    assert not any(t.startswith("0x") for t in texts(heading))


def test_progress_is_a_bar_and_not_a_sentence() -> None:
    view = portfolio_view()
    view.progress_to(0.5)

    assert view.progress.visible and view.progress.value == 0.5
    assert not [t for t in texts(view) if "%" in t or "Checking" in t]

    view.progress_to(1.0)
    assert not view.progress.visible


def test_the_table_wears_the_theme_like_the_pool_list_does() -> None:
    from ui import theme

    chad = portfolio_view(ThemedPage("chad"))
    plain = portfolio_view(ThemedPage("light"))

    assert chad._table.shadow is theme.PANEL_SHADOW
    assert chad._table.border is not None
    assert chad._header.bgcolor == theme.RULE
    assert chad._rows_box.theme.hover_color == theme.HOVER
    assert plain._table.shadow is None and plain._table.border is None


def test_a_theme_change_reaches_the_portfolio_too() -> None:
    from ui import theme

    view = portfolio_view(ThemedPage("light"))
    view._page.theme, view._page.theme_mode = theme.theme_for("chad")
    view.rebuild()
    assert view._table.shadow is theme.PANEL_SHADOW
