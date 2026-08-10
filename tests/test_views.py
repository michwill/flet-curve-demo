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
    # What the chain has, not what has been fetched: the count is about
    # the chain, and one that climbed as you scrolled was answering a
    # question about the paging instead.
    assert view.count_label.value == "7 pools"

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
    view.page_scrolled(far)
    assert len(view.rows.controls) == 3  # nowhere near the end; no fetch

    near = SimpleNamespace(pixels=99_000.0, max_scroll_extent=99_100.0)
    view.page_scrolled(near)
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


def test_the_search_box_and_the_sort_picker_are_cut_the_same() -> None:
    """On a phone they are the only two controls on that row, and two form
    fields cut differently read as two kinds of thing.

    The height cannot be asserted the way the corner can: neither control
    honours `height`, the dropdown will not go under Material's 48px, and
    the text field's own height comes out of its padding -- so the number
    here is one measured against the running app (both 48px, tops and
    bottoms level) and the padding is what holds it."""
    from ui.pool_list import FIELD_INSET, FIELD_RADIUS

    view = PoolListView(StubPage(), on_open=lambda _p: None)

    assert view.search.border_radius == view.sort_picker.border_radius == FIELD_RADIUS
    assert view.search.content_padding.top == FIELD_INSET
    assert view.search.content_padding.bottom == FIELD_INSET


async def test_the_count_is_not_shown_on_a_phone() -> None:
    """That row already carries the search box and the sort picker, and
    how many pools a chain has is not what anyone is there to read."""
    view = PoolListView(StubPage(), on_open=lambda _p: None)
    view.attach(FakeFeed([make_pool() for _ in range(7)], page_size=3))
    await view.load_more()

    view.set_layout(layout_for(LAPTOP))
    assert view.count_label.visible and view.count_label.value == "7 pools"

    view.set_layout(layout_for(PHONE))
    assert not view.count_label.visible


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
    # Deposit, Withdraw, Swap, Stake, Claim. Two of those are conditional
    # on what the wallet holds; `tabs` is the full set either way, and
    # what the bar draws is `_sync_tabs`' business.
    assert len(view.tabs) == 5


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


def test_a_button_is_square_ish_and_outlined_under_chad() -> None:
    """Material's own is a stadium with a tonal fill and no edge, which
    among this theme's bordered boxes reads as a borrowed control."""
    from ui import buttons

    style = buttons.style(ThemedPage("chad"))

    assert style is not None
    assert style.shape.radius == buttons.RADIUS < 10
    assert _state(style.side, ft.ControlState.DEFAULT).width == 1
    # No elevation: the shadow is the wrapper's, and Material's own would
    # be a second one with a gradient in it.
    assert style.elevation == 0


def _state(value: object, state: ft.ControlState):
    """One entry of a ButtonStyle's per-state map, whatever its type."""
    assert isinstance(value, dict)
    return value[state]


def _channels(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def test_a_disabled_button_says_what_colour_it_is_in_full() -> None:
    """Any style at all costs a Flet button its disabled colours, and a
    scheme colour with opacity does not resolve inside a state map -- so
    the fill and the label are literal, or Deposit sits there in slate
    grey looking like something went wrong."""
    from ui import buttons

    style = buttons.style(ThemedPage("chad"))
    off = ft.ControlState.DISABLED

    for value in (_state(style.bgcolor, off), _state(style.color, off)):
        assert value.startswith("#") and len(value) == 7
    # Material's own recipe: 12% of the body colour for the fill, 38% for
    # the label on top of it. What matters is that the fill stays light --
    # it reads as "not yet", where the slate Flutter falls back to (#8D8E8E)
    # reads as a warning -- and that the label is still darker than it.
    fill = _channels(buttons.DISABLED_FILL)
    assert min(fill) > 0xC0
    assert max(_channels(buttons.DISABLED_TEXT)) < min(fill)
    on = ft.ControlState.DEFAULT
    assert _state(style.bgcolor, on) == ft.Colors.SURFACE_CONTAINER_LOW


def test_elsewhere_a_button_is_left_to_material() -> None:
    from ui import buttons

    assert buttons.style(ThemedPage("light")) is None
    assert buttons.style(ThemedPage("dark")) is None


def test_the_action_buttons_cast_the_hard_shadow_under_chad() -> None:
    from ui import theme
    from ui.actions import DepositTab

    chad = DepositTab(ThemedPage("chad"), make_pool(), lambda: None, None)
    plain = DepositTab(ThemedPage("light"), make_pool(), lambda: None, None)

    for tab, expected in ((chad, theme.INSET_SHADOW), (plain, None)):
        for box in (tab._approve_box, tab._submit_box):
            box.before_update()
            assert box.shadow is expected


def test_a_wrapped_button_takes_no_room_while_it_is_hidden() -> None:
    """Flet skips an invisible control, so a hidden Approve step costs
    nothing -- but a *wrapper* around one is still a child of the column
    and still takes its spacing. The gap would sit there until an approval
    was needed."""
    from ui import buttons

    button = ft.Button("1. Approve", visible=False)
    box = buttons.shadowed(button, ThemedPage("chad"))

    box.before_update()
    assert box.visible is False

    button.visible = True
    box.before_update()
    assert box.visible is True


def test_a_button_takes_the_new_theme_without_being_rebuilt() -> None:
    """The header is built once and outlives every theme switch, so the
    wrapper reads the page rather than remembering what it was born in."""
    from ui import buttons, theme

    page = ThemedPage("light")
    box = buttons.shadowed(ft.Button("Connect wallet"), page)

    box.before_update()
    assert box.shadow is None

    page.theme, page.theme_mode = theme.theme_for("chad")
    box.before_update()
    assert box.shadow is theme.INSET_SHADOW


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


def widths_in(control) -> list[float]:
    """Every fixed width in a subtree. A cell with one cannot flex down."""
    found: list[float] = []

    def walk(node, seen):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ft.Container) and node.width:
            found.append(node.width)
        if isinstance(node, ft.Control):
            for name in node.__dataclass_fields__:
                walk(getattr(node, name, None), seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, seen)

    walk(control, set())
    return found


def test_the_pool_name_gets_its_own_line_on_a_phone() -> None:
    """The name was the only thing in that row without a width of its own,
    so it took what the back button, the marks and the two figures left --
    about ten pixels, which Flutter filled one letter at a time. Vertical
    `DAI/USDC/USDT`."""
    view = detail_view()
    view.set_layout(layout_for(PHONE))

    header = view._header_slot.content
    assert isinstance(header, ft.Column)          # stacked, not one row
    name_row = header.controls[0]
    # The name shares its line with the back button and the marks, and
    # nothing else: no figure competing for the width.
    assert "TVL" not in texts(name_row)
    assert make_pool().display_name in texts(name_row)
    # And the figures are still there, below it.
    assert "TVL" in texts(header)


def test_a_wide_page_keeps_the_header_on_one_line() -> None:
    view = detail_view()
    view.set_layout(layout_for(LAPTOP))
    assert isinstance(view._header_slot.content, ft.Row)


def test_the_title_is_smaller_where_the_page_is_narrow() -> None:
    from ui.typography import TITLE, TITLE_NARROW

    wide, narrow = detail_view(), detail_view()
    wide.set_layout(layout_for(LAPTOP))
    narrow.set_layout(layout_for(PHONE))

    def title_size(view) -> float:
        name = make_pool().display_name
        return next(
            node.size
            for node in _all_texts(view._header_slot)
            if node.value == name
        )

    assert title_size(wide) == TITLE
    assert title_size(narrow) == TITLE_NARROW < TITLE


def _all_texts(control) -> list:
    found: list = []

    def walk(node, seen):
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, ft.Text):
            found.append(node)
        if isinstance(node, ft.Control):
            for name in node.__dataclass_fields__:
                walk(getattr(node, name, None), seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, seen)

    walk(control, set())
    return found


def test_the_composition_drops_its_columns_on_a_phone() -> None:
    """Price, share and balance are 340px of fixed width between them,
    which leaves the asset column setting its symbol vertically -- the same
    fault as the title, in the same place."""
    view = detail_view()
    view.pool.merge_detail(
        {
            "n_coins": 2,
            "balances": [1000.0, 2000.0],
            "coins": [
                {"symbol": "C0", "address": "0x" + "00" * 20, "decimals": 18,
                 "usd_price": 1.0},
                {"symbol": "C1", "address": "0x" + "01" * 20, "decimals": 18,
                 "usd_price": 1.0},
            ],
        }
    )
    view._composition_slot.content = view._composition()
    wide = widths_in(view._composition_slot)

    view.set_layout(layout_for(PHONE))
    view._composition_slot.content = view._composition()
    narrow = widths_in(view._composition_slot)

    # A column is 80px or more; anything smaller is a token mark, which is
    # meant to have a size.
    assert max(wide) >= 150                       # the balance column
    assert [w for w in narrow if w >= 80] == []   # cards: no columns at all
    # The numbers are still all there, just captioned rather than columned.
    assert "Price" in texts(view._composition_slot)


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


def test_nothing_in_the_action_panel_is_given_a_height() -> None:
    """It used to be: `TabBarView` cannot size to its content, so the
    panel was told how tall it was, from a number worked out from how many
    coins a pool has and how tall a row measures on one machine. On a
    phone, with another font and another OS, the content came out taller
    and the Deposit button was drawn over the slippage box.

    A guessed size cannot be calibrated into a right one -- it can only be
    removed. Every height in the panel now comes from what is in it."""
    view = detail_view()

    assert view._right.height is None
    assert view._tab_body.height is None
    assert view._tab_bar.height is None
    for tab in view.tabs:
        assert tab.control.expand is None      # nothing to fill
        assert tab.control.scroll is None      # and nothing to scroll
        assert tab.frame.height is None


def test_the_tab_bar_is_containers_rather_than_flets_tabs() -> None:
    """`TabBarView` is the only thing that renders a Flet tab body, and it
    is the thing that needs the height -- so the bar is built by hand. And
    from containers, not buttons: a `TextButton` in this app hovers
    correctly and never fires its handler in a published web build."""
    view = detail_view()
    labels = view._tab_bar.content.controls

    # One per tab that has something to act on. With no wallet the Stake
    # panel reads no balances, so it stays out -- see `ActionTab.available`.
    assert len(labels) == len([tab for tab in view.tabs if tab.available])
    for label in labels:
        assert isinstance(label, ft.Container)
        assert label.on_click is not None
    # And they wrap rather than scroll, so a wider font takes a second
    # line instead of putting a scrollbar in the panel.
    assert view._tab_bar.content.wrap is True


def test_stake_joins_the_bar_once_there_is_a_position() -> None:
    """The bar is a function of the balances, re-read after every confirmed
    transaction -- so depositing into a pool you had nothing in makes the
    Stake tab appear rather than leaving it there saying "0 LP"."""
    view = detail_view()
    titles = lambda: [  # noqa: E731 - read once, in the asserts below
        label.content.value for label in view._tab_bar.content.controls
    ]
    assert "Stake" not in titles()

    stake = next(tab for tab in view.tabs if tab.title == "Stake")
    stake.lp_balance = 10**18
    view._sync_tabs()
    assert "Stake" in titles()


def test_the_panel_you_are_on_survives_a_tab_appearing() -> None:
    """Indices are into `tabs`, not into what is drawn -- otherwise a tab
    appearing to the left of the one you are on would slide it sideways."""
    view = detail_view()
    view._show_tab(2)  # Swap
    assert view._tab_body.content is view.tabs[2].frame

    next(tab for tab in view.tabs if tab.title == "Stake").lp_balance = 10**18
    view._sync_tabs()
    assert view._tab == 2
    assert view._tab_body.content is view.tabs[2].frame


def test_leaving_a_tab_that_has_nothing_left_to_do() -> None:
    """Unstaking the last of a position takes Stake out from under you."""
    view = detail_view()
    stake_index = next(i for i, tab in enumerate(view.tabs) if tab.title == "Stake")
    view.tabs[stake_index].staked = 10**18
    view._sync_tabs()
    view._show_tab(stake_index)
    assert view._tab == stake_index

    view.tabs[stake_index].staked = 0
    view._sync_tabs()
    assert view._tab == 0, "fell back to the first tab that has something to do"


def test_tapping_a_label_shows_that_panel() -> None:
    view = detail_view()
    assert view._tab_body.content is view.tabs[0].frame

    view._show_tab(2)
    assert view._tab_body.content is view.tabs[2].frame
    assert view._tab == 2


def test_the_tab_you_are_on_is_marked() -> None:
    """Underlined as well as coloured, as the header's page links are:
    colour alone is a weak signal."""
    view = detail_view()
    view._show_tab(1)
    labels = view._tab_bar.content.controls

    assert labels[1].border is not None
    assert labels[0].border is None
    assert labels[1].content.color != labels[0].content.color


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
    # `_sync_nav` fills the menu as well as the links -- the two say the
    # same thing at different widths.
    app._icons = False
    app._totals = []
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


# -- a header that fits a phone --------------------------------------------


def header_app(width: float):
    """`CurveApp` with the real header, laid out for a window this wide.

    On the headless session rather than `StubPage`: `_build` sets a window
    size, and a stub with no `window` is a stub that cannot hold a header.
    """
    import main as app_module
    from tests.fake_session import Session

    app_module.autoconnect = lambda: False
    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = Session(width=width)
    app.chain = "ethereum"
    app.chains = {}
    app.wallet = None
    app._page_name = "pools"
    app._detail = None
    app._address_expanded = False
    app._route_applied = True
    app.storage = None
    app.api = None
    app.feed = None
    app._build()
    app._apply_layout(width)
    return app


def test_a_phone_header_drops_every_label() -> None:
    """It did not fit: at 390px the picker's network name and the connect
    button's label ran off the right-hand edge, leaving "Conne" and then
    nothing at all."""
    app = header_app(PHONE)

    assert app._icons is True
    # The picker keeps its mark and loses the name -- `text` is what the
    # closed field shows, `content` is what the open menu draws.
    assert [option.text for option in app.chain_picker.options] == [""] * len(
        app.chain_picker.options
    )
    assert app.chain_picker.width < 100
    # And no box drawn round it: a form field's outline says "there is a
    # value in here", which a bare mark does not need saying about.
    assert app.chain_picker.border == ft.InputBorder.NONE
    assert not app.theme_button.visible
    assert app.menu.visible


def _option_label(option: ft.DropdownOption) -> str:
    """The name the open menu draws for one network."""
    content = option.content
    if isinstance(content, ft.Row):
        texts = [c.value for c in content.controls if isinstance(c, ft.Text)]
        return texts[0] if texts else ""
    return content.value if isinstance(content, ft.Text) else ""


def test_the_address_chip_is_built_without_a_width() -> None:
    """And animates to its content instead of between two of them.

    `animate_size` rather than `animate`: the latter animates properties
    the control is given, and this one is deliberately not given a width,
    so the hover would jump rather than grow. See `_show_account`.
    """
    app = header_app(LAPTOP)

    assert app.account_chip.width is None
    assert app.account_chip.animate_size is not None


def test_the_open_menu_still_names_every_network_on_a_phone() -> None:
    """Dropping the label is about the closed field, not the menu.

    A `Dropdown`'s menu takes the field's width unless it is given one of
    its own, so on a phone the 78px field cropped every name in the menu
    away and left a column of unlabelled circles -- nothing to choose
    between, on the one screen whose whole job is choosing.
    """
    app = header_app(PHONE)

    assert app.chain_picker.menu_width > app.chain_picker.width
    assert all(_option_label(option) for option in app.chain_picker.options)
    assert "Ethereum" in [_option_label(o) for o in app.chain_picker.options]


def test_the_menu_is_the_same_width_whatever_the_header_does() -> None:
    """The field follows the header in and out of icons; the menu does
    not, because you are reading names in it either way."""
    assert header_app(PHONE).chain_picker.menu_width == (
        header_app(LAPTOP).chain_picker.menu_width
    )


def test_a_laptop_header_keeps_them() -> None:
    app = header_app(LAPTOP)

    assert app._icons is False
    assert all(option.text for option in app.chain_picker.options)
    assert app.chain_picker.border == ft.InputBorder.OUTLINE
    assert app.theme_button.visible
    assert not app.menu.visible


def test_every_theme_in_the_menu_carries_its_own_face() -> None:
    """The button it replaces on a phone shows one; a list of three words
    with nothing beside them would not read as the same setting."""
    from ui import theme as themes

    app = header_app(PHONE)
    themed = [item for item in app.menu.items if item.icon is not None]

    assert len(themed) == len(themes.NAMES)
    # Chad is a picture and the other two are glyphs, which is why the
    # item takes a control rather than an icon name.
    kinds = {type(item.icon) for item in themed}
    assert ft.Image in kinds and ft.Icon in kinds


def test_the_menu_and_the_button_draw_a_theme_the_same_way() -> None:
    """Two pictures for one setting would read as two settings."""
    app = header_app(LAPTOP)
    for name in ("light", "dark", "chad"):
        on_button = app._theme_mark(name)
        in_menu = app._theme_mark(name, 20)
        assert type(on_button) is type(in_menu)


def test_the_themes_move_into_the_menu_on_a_phone() -> None:
    """Cycling light -> dark -> Chad is fine while the button is on screen
    saying where you are; folded away it would be a mystery tap. So the
    menu names all three and ticks the one you are in."""
    from ui import theme as themes

    app = header_app(PHONE)
    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]

    assert "Pools" in labels and "Portfolio" in labels
    for name in themes.NAMES:
        assert f"{name.capitalize()} theme" in labels
    # One tick per group: the theme you are in, and (below) the page you
    # are on.
    themed = [
        item for item in app.menu.items
        if item.content is not None and "theme" in item.content.value
    ]
    assert [item.checked for item in themed].count(True) == 1


def test_the_chain_totals_move_into_the_menu_on_a_phone() -> None:
    """The header drops them at card widths -- there is no room beside a
    hamburger and a chain -- and they are the whole of what that line
    said, so they go where the rest of the header went."""
    app = header_app(PHONE)
    app._totals = [("TVL", "$1.38b"), ("24h volume", "$89.61m")]
    app.menu.items = app._menu_items()

    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]
    # Last, under everything that is somewhere to go.
    assert labels[-2:] == ["TVL $1.38b", "24h volume $89.61m"]
    # Figures, not destinations.
    figures = [item for item in app.menu.items if item.content is not None][-2:]
    assert all(item.disabled and item.on_click is None for item in figures)


def test_the_figures_reach_the_menu_when_they_arrive() -> None:
    """They land a request after the menu was built, so setting them has
    to rebuild it -- otherwise the phone's menu keeps the two lines it was
    built with, which is none."""
    app = header_app(PHONE)
    app._show_totals({"tvl": 1.38e9, "volume": 8.961e7})

    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]
    assert labels[-2] == "TVL $1.38b"
    assert labels[-1] == "24h volume $89.61m"
    # And the bar still says it the way it always did, for wider windows.
    assert app.totals.value.startswith("TVL $1.38b")


def test_a_lite_chain_reports_no_volume_in_the_menu_either() -> None:
    """Nothing counts trades there, and "24h volume $0.00" would read as a
    quiet day rather than as a measurement nobody takes."""
    app = header_app(PHONE)
    app._show_totals({"tvl": 1.0e6, "volume": None})

    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]
    assert labels[-1] == "TVL $1.00m"
    assert not any("volume" in label for label in labels)


def test_a_chain_with_no_totals_yet_lists_none() -> None:
    """They arrive a request after the menu is first built, and an empty
    "TVL" with nothing after it is worse than no line."""
    app = header_app(PHONE)
    app._totals = []
    app.menu.items = app._menu_items()

    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]
    assert labels[0] == "Pools"


def test_the_menu_says_which_page_you_are_on() -> None:
    """It is the only thing that does once the links are gone -- the wide
    header underlines the current page, and a phone has no header to
    underline."""
    app = header_app(PHONE)

    app._page_name = "pools"
    app._sync_nav()
    ticks = {
        item.content.value: item.checked
        for item in app.menu.items
        if item.content is not None and item.content.value in ("Pools", "Portfolio")
    }
    assert ticks == {"Pools": True, "Portfolio": False}

    app._page_name = "portfolio"
    app._sync_nav()
    ticks = {
        item.content.value: item.checked
        for item in app.menu.items
        if item.content is not None and item.content.value in ("Pools", "Portfolio")
    }
    assert ticks == {"Pools": False, "Portfolio": True}


def test_a_laptop_menu_is_pages_only() -> None:
    app = header_app(LAPTOP)
    labels = [
        item.content.value for item in app.menu.items if item.content is not None
    ]
    assert labels == ["Pools", "Portfolio"]


def test_crossing_the_breakpoint_repaints_the_header(monkeypatch) -> None:
    """The views repaint themselves and the header does not. Until it was
    told to, reaching 390px by resizing left the picker still spelling out
    "Ethereum" and the theme button still on the row -- while the table
    below reflowed into cards, because a list view updates itself. Opening
    narrow looked right, which is what made it easy to miss."""
    import main as app_module

    app = header_app(LAPTOP)
    painted: list = []
    monkeypatch.setattr(app_module, "safe_update", painted.append)

    app._apply_layout(PHONE)

    assert app.header in painted


def test_connect_is_an_icon_at_every_width() -> None:
    """It used to be a labelled button on a wide window, and that button
    never matched the height of the network picker beside it -- 33px of
    pill against a 46px field, with no way to reconcile them that was not
    a guessed number. An icon has no frame to disagree about, and it is
    what the theme button next to it was already doing."""
    for width in (LAPTOP, PHONE):
        app = header_app(width)
        app.connect_button.visible = True
        app.connect_icon.before_update()

        assert app.connect_icon.visible, f"missing at {width}px"

    # And the labelled button is not on the bar at all any more.
    assert not hasattr(header_app(LAPTOP), "connect_box")


def test_the_icon_says_what_the_button_would_have_said() -> None:
    """The label is where the action reports itself -- "Connecting..."
    rather than "Connect wallet" -- and with no labelled button drawn, the
    tooltip is the only place left for that sentence."""
    import main as app_module

    app = header_app(LAPTOP)
    app.connect_button.visible = True

    app.connect_icon.before_update()
    assert app.connect_icon.tooltip == app_module.CONNECT_LABEL

    app.connect_button.content = "Connecting…"
    app.connect_icon.before_update()
    assert app.connect_icon.tooltip == "Connecting…"


def test_the_stand_in_disappears_with_the_button_it_stands_for() -> None:
    """`visible` is set on the connect button from a dozen places that
    know nothing about layout."""
    app = header_app(PHONE)
    app.connect_button.visible = False
    app.connect_icon.before_update()
    assert not app.connect_icon.visible

    app.connect_button.visible = True
    app.connect_button.disabled = True
    app.connect_icon.before_update()
    assert app.connect_icon.visible and app.connect_icon.disabled


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


def test_the_portfolio_has_no_progress_bar_of_its_own() -> None:
    """It uses the app's, in the strip under the top bar -- the same place
    the pool list fills. A bar inside the page would push the table down
    as it appeared and pull it back up as it went."""
    view = portfolio_view()
    view.show([make_holding()])

    assert not any(isinstance(c, ft.ProgressBar) for c in _all_controls(view))
    # The share column is a percentage; a *progress* percentage is not.
    assert not [t for t in texts(view) if "Checking" in t or "Loading" in t]


def test_the_app_bar_carries_the_portfolio_progress() -> None:
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.progress = ft.ProgressBar(visible=False)

    app.loading(0.5)
    assert app.progress.visible and app.progress.value == 0.5

    app.loading()                      # indefinite, as the pool list uses it
    assert app.progress.value is None

    app.loaded()
    assert not app.progress.visible


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


# -- the claim bar ----------------------------------------------------------


def earning(**kw):
    from curve.earnings import Earning

    base = {"pool": "0x" + "11" * 20, "gauge": "0x" + "22" * 20, "staked": 1000}
    base.update(kw)
    return Earning(**base)


def crv_reward(amount: float):
    from curve.earnings import Reward

    return Reward("", "CRV", 18, int(amount * 10**18), 0.5, minted=True)


def arb_reward(amount: float, price: float = 1.5):
    from curve.earnings import Reward

    return Reward("0x" + "ab" * 20, "ARB", 18, int(amount * 10**18), price)


def test_the_claim_buttons_are_dressed_by_the_theme_they_end_up_in() -> None:
    """Built before the remembered theme is applied, so a style read once
    at construction is Material's -- and a stadium among Chad's boxes is a
    control borrowed from another program."""
    from ui import buttons, theme

    page = ThemedPage("light")
    view = portfolio_view(page)
    assert view.claim_crv.style is None

    page.theme, page.theme_mode = theme.theme_for("chad")
    view.claim_crv.before_update()

    assert view.claim_crv.style is not None
    assert view.claim_crv.style.shape.radius == buttons.RADIUS


def test_the_claim_buttons_say_what_they_would_claim() -> None:
    """"Claim 1.23 CRV", not "Claim CRV". The button commits an address to
    a transaction, and it should say what that transaction is for."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(crv_reward(1.23), arb_reward(4.0)))], chain_id=1)

    assert view.claim_crv.content == "Claim 1.23 CRV"
    assert view.claim_rewards.content == "Claim rewards ($6.00)"


def test_the_crv_on_the_button_is_the_whole_portfolio_s() -> None:
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings(
        [
            earning(pool="0x" + "11" * 20, rewards=(crv_reward(1.5),)),
            earning(pool="0x" + "33" * 20, rewards=(crv_reward(2.25),)),
        ],
        chain_id=1,
    )

    assert view.claim_crv.content == "Claim 3.75 CRV"


def test_a_dust_amount_goes_back_to_naming_the_token() -> None:
    """CRV accrues every block, so a mint is followed immediately by a few
    hundred wei owed again -- too little to print, and "Claim 0 CRV" makes
    the claim that just landed look like it did not."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(crv_reward(0.00000013),))], chain_id=1)

    assert view.claim_crv.content == "Claim CRV"


def test_a_portfolio_too_big_for_one_mint_says_how_many_sends() -> None:
    """`mint_many(address[8])` on Ethereum, so ten gauges is two sends --
    and an unannounced second wallet prompt looks like being asked to sign
    the same thing twice."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings(
        [earning(pool=f"0x{i:040x}", gauge=f"0x{i + 1:040x}", rewards=(crv_reward(1.0),))
         for i in range(10)],
        chain_id=1,
    )

    assert view.claim_crv.content == "Claim 10 CRV (2 txs)"


def test_the_same_ten_gauges_are_one_send_where_the_array_is_bigger() -> None:
    """Thirty-two slots on the sidechain factories, so no count at all."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings(
        [earning(pool=f"0x{i:040x}", gauge=f"0x{i + 1:040x}", rewards=(crv_reward(1.0),))
         for i in range(10)],
        chain_id=42161,
    )

    assert view.claim_crv.content == "Claim 10 CRV"


def test_crv_owed_where_there_is_no_minter_offers_no_button() -> None:
    """X Layer has gauges and no Minter. "Something is owed" and "this
    button can send something" are different questions, and the button
    answers the second one."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(crv_reward(5.0), arb_reward(1.0)))], chain_id=196)

    assert view.claim_crv.visible is False
    assert view.claim_rewards.visible is True


def test_an_unpriced_reward_drops_the_value_rather_than_showing_zero() -> None:
    """`Claim rewards ($0)` reads as a button that does nothing, and the
    tokens are there whether or not the API published a price for them."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(arb_reward(4.0, price=0.0),))], chain_id=1)

    assert view.claim_rewards.content == "Claim rewards"
    assert view.claim_rewards.visible is True


class ClaimingChain:
    """A chain that pays out: it answers the reads, and mines the send.

    The two rounds of reads are answered from `owed`, which the send
    empties -- so a test can watch the page's own numbers cross from
    "before the claim" to "after" without a fork.
    """

    def __init__(self, gauge: str, token: str, owed: int) -> None:
        self.gauge = gauge
        self.token = token
        self.owed = owed
        self.sent: list[dict] = []
        self.round = 0

    async def call(self, _to: str, _data: str) -> str:
        from .test_parameters import aggregate3_response

        # `read_earnings` asks three rounds in a fixed order, each
        # depending on the last: the per-gauge numbers and how many reward
        # tokens there are, then which token that is, then what is owed in
        # it. One gauge here, so one call in each of the last two.
        answers = [
            [400, 0, 1],                 # working balance, CRV owed, count
            [int(self.token, 16)],       # which token
            [self.owed],                 # what it owes
        ][self.round % 3]
        self.round += 1
        return aggregate3_response(answers)

    async def send_transaction(self, tx: dict) -> str:
        self.sent.append(tx)
        self.owed = 0
        return "0x" + "ab" * 32


async def test_a_confirmed_claim_updates_the_numbers_it_was_made_against(
    monkeypatch,
) -> None:
    """The page must not go on showing what was owed before the claim.

    Re-read rather than reloaded: a claim moves reward tokens and not LP,
    so every position is exactly as it was, and rescanning the chain for
    positions that cannot have changed is the slow way to learn nothing.
    """
    import main as app_module
    from curve.earnings import Earning

    gauge = "0x" + "22" * 20
    token = "0x" + "ab" * 20
    chain = ClaimingChain(gauge, token, 4 * 10**18)

    monkeypatch.setattr(app_module, "wait_for_confirmation", _mined)

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.chain = "ethereum"
    app.chains = {"ethereum": 1}
    app.portfolio_view = portfolio_view(app.page)
    app.portfolio_view.show([make_holding()])
    app.wallet = SimpleNamespace(address="0x" + "11" * 20, provider=chain)
    seed = Earning(pool="0x" + "11" * 20, gauge=gauge, staked=1000)
    app._earning_seeds = ([seed], {token: ("ARB", 18, 1.5)}, 0.5, 1)
    await app.reread_earnings(app.wallet.address, chain)

    assert app.portfolio_view.claim_rewards.content == "Claim rewards ($6.00)"
    assert app.portfolio_view.accrued_label.value == "Unclaimed rewards:"
    assert app.portfolio_view.accrued_value.value == "$6.00"

    await app.claim_portfolio(False)

    assert len(chain.sent) == 1
    assert app.portfolio_view.claim_rewards.visible is False
    assert app.portfolio_view.accrued_label.value == ""
    assert app.portfolio_view.accrued_value.value == ""


async def _mined(_provider, _tx, **_kw) -> dict:
    return {"status": "0x1"}


def test_the_apr_column_is_a_rate_and_not_also_a_multiplier() -> None:
    """The boost is an input to this number, not a second number. A column
    answering "what am I earning" with two figures makes the reader work
    out which one is the answer."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings(
        [earning(staked=1000, working=1000, crv_apr=10.0, rewards=(crv_reward(1.0),))],
        chain_id=1,
    )

    shown = view.rows.controls[0]._apr.value
    assert shown == "25.00%"          # 10% at the 2.5x ceiling
    assert "x" not in shown


def test_what_is_owed_is_set_like_the_total_it_belongs_to() -> None:
    """Words in the body colour, figure bold -- the same pairing as
    "Total value:", because it is the same kind of statement about the
    same portfolio."""
    import flet as ft

    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(arb_reward(3.0, price=2.0),))], chain_id=1)

    assert view.accrued_label.value == "Unclaimed rewards:"
    assert view.accrued_label.weight != ft.FontWeight.BOLD
    assert view.accrued_value.value == "$6.00"
    assert view.accrued_value.weight == ft.FontWeight.BOLD


def test_nothing_priced_leaves_the_figure_off_rather_than_showing_zero() -> None:
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(arb_reward(3.0, price=0.0),))], chain_id=1)

    assert view.accrued_label.value == "Unclaimed rewards"
    assert view.accrued_value.value == ""
    assert view._claim_bar.visible is True


def test_a_new_wallet_does_not_inherit_the_last_one_s_claim() -> None:
    """Not a stale number -- the wrong account's. The earnings pass is the
    slowest of the three reads, so the window in which it would be on
    screen is the longest one on the page."""
    view = portfolio_view()
    view.show([make_holding()])
    view.show_earnings([earning(rewards=(crv_reward(5.0), arb_reward(2.0)))], chain_id=1)
    assert view._claim_bar.visible is True

    view.forget_earnings()
    view.show([make_holding()])

    assert view._claim_bar.visible is False
    assert view.rows.controls[0]._rewards.value == "\u2013"


async def test_pool_payloads_are_asked_for_together_not_in_turn() -> None:
    """An address in forty gauges waited forty round trips for two columns
    and a claim button, which is long enough that the page looked like it
    had decided there was nothing to show."""
    import main as app_module

    running, high_water = 0, 0

    class Api:
        async def pool_detail(self, _chain_id, _address):
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            await asyncio.sleep(0.01)
            running -= 1
            return {"crv_apr": 3.0, "extra_rewards_apr": []}

        async def usd_price(self, _chain, _address):
            return 0.5

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.chain = "ethereum"
    app.api = Api()
    app._earnings = []
    app._earning_seeds = None
    app._details = asyncio.Semaphore(app_module.EARNINGS_REQUESTS)
    app.portfolio_view = portfolio_view(app.page)

    async def reread(_account, _provider) -> None:
        pass

    app.reread_earnings = reread      # type: ignore[method-assign]
    holdings = [
        make_holding(address=f"0x{i:040x}", gauge=f"0x{i + 1:040x}", staked=10**18)
        for i in range(20)
    ]

    await app.load_earnings(holdings, "0x" + "11" * 20, 1, object())

    assert high_water == app_module.EARNINGS_REQUESTS
    seeds, _meta, _price, _chain = app._earning_seeds
    assert len(seeds) == 20
    assert all(seed.crv_apr == 3.0 for seed in seeds)


async def test_declining_a_claim_leaves_no_red_line_behind() -> None:
    """The user dismissed the wallet. They know that; saying it back in
    red reports a failure that did not happen."""
    import main as app_module
    from wallet.base import RpcError

    class Refusing:
        async def send_transaction(self, _tx: dict) -> str:
            raise RpcError(4001, "User rejected the request")

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.chain = "ethereum"
    app.chains = {"ethereum": 1}
    app.portfolio_view = portfolio_view(app.page)
    app.wallet = SimpleNamespace(address="0x" + "11" * 20, provider=Refusing())
    app._earnings = [
        earning(rewards=(arb_reward(4.0),)),
    ]

    await app.claim_portfolio(False)

    assert app.portfolio_view.claim_status.value == ""
    assert app.portfolio_view.status.visible is False


async def test_losing_the_wallet_reloads_the_portfolio() -> None:
    """Whoever's positions those were, they are not on screen for a page
    with no wallet behind it. The same reload hangs off connecting,
    restoring and switching account -- each changes the answer, and the
    page showing it is often the page you are on."""
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.wallet = object()
    app._page_name = "portfolio"
    app.connect_button = ft.Button("Connect")
    app._show_account = lambda **_kw: None      # type: ignore[method-assign]
    reloaded = []

    async def load_portfolio() -> None:
        reloaded.append(True)

    app.load_portfolio = load_portfolio      # type: ignore[method-assign]

    await app._wallet_gone()

    assert app.wallet is None
    assert app.connect_button.visible is True
    assert reloaded == [True]


def test_token_marks_are_resampled_with_mipmaps() -> None:
    """`high` is bicubic, which is for *magnifying*; these are minified
    tenfold and came out noisy. `medium` is the mipmapped one -- see
    `ui.logos.SAMPLING`."""
    from curve.models import Coin
    from ui.logos import token_mark

    mark = token_mark(Coin("0x" + "11" * 20, "CRV", 18), "ethereum", 27)
    images = [c for c in _all_controls(mark) if isinstance(c, ft.Image)]
    for image in images:
        assert image.filter_quality == ft.FilterQuality.MEDIUM


def test_coin_marks_are_painted_right_to_left() -> None:
    """So each disc is under the one before it, not over it.

    A `Stack` paints in order. The natural way round puts every coin's
    left edge on top of its neighbour, and a logo with artwork in its
    top-left corner then floats that corner in the middle of the seam --
    tacETH carries an Ethereum badge there, and it read as a third coin
    in the pool."""
    from curve.models import Coin
    from ui.logos import coin_stack

    coins = [Coin("0x" + f"{n:02x}" * 20, f"C{n}", 18, index=n) for n in range(3)]
    stack = coin_stack(coins, "ethereum", 27).content

    # Left-most last in the paint order means left-most on top.
    assert [mark.left for mark in stack.controls] == sorted(
        [mark.left for mark in stack.controls], reverse=True
    )


def test_a_theme_change_reaches_both_tables() -> None:
    """The portfolio is built at startup and the saved theme arrives
    later, so left untold it keeps the first theme's header band, border,
    shadow and hover -- while the pool list wears the new one. Two tables
    meant to be the same table."""
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = ThemedPage("chad")
    app.header = ft.Container()
    app.account_chip = ft.Container()
    app.connect_button = ft.Button("Connect")
    app._detail = None
    app.list_view = PoolListView(app.page, on_open=lambda _p: None)
    app.portfolio_view = portfolio_view(app.page)

    # Both were built light; the page is Chad now.
    app.portfolio_view._table.shadow = None
    app.portfolio_view._header.bgcolor = None

    app._rebuild_view()

    from ui import theme

    assert app.list_view._table.shadow is theme.PANEL_SHADOW
    assert app.portfolio_view._table.shadow is theme.PANEL_SHADOW
    assert app.portfolio_view._table.border is not None
    assert app.portfolio_view._header.bgcolor == theme.RULE
    assert app.portfolio_view._rows_box.theme.hover_color == theme.HOVER
