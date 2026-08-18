"""The pool list: search, sortable columns, one row per pool.

Sorted by 24h volume by default, matching Curve's own UI.

Everything about the ordering happens on the server. The v2 API caps a page
at 50 rows, so the list is a cursor rather than a snapshot: the first page
paints after one request and the rest load as the list scrolls. A client
cannot correctly sort or search a list it has not fully loaded, so changing
either resets the cursor and asks the server again -- which has the pleasant
side effect that the top of the list is always the true top, not the top of
whatever happened to be in memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import flet as ft

from curve.api import PoolFeed
from curve.format import apr_range, compact_usd, percent
from curve.models import Coin, Pool
from curve.rewards import crv_token
from curve.sort import DEFAULT_SORT, SORTS

from . import AnyEvent, safe_update, theme
from .logos import MARK_SIZE, pool_stack, token_mark
from .responsive import Layout, layout_for
from .typography import BODY, LABEL, ROW_TITLE, SMALL

#: Column widths, shared by the header and every row so they line up.
W_BASE = 110
W_REWARDS = 190
W_VOLUME = 130
W_TVL = 130

COLUMN_WIDTH = {
    "base": W_BASE,
    "incentives": W_REWARDS,
    "volume": W_VOLUME,
    "tvl": W_TVL,
}
#: Columns that measure trading, which is the one thing a Curve Lite
#: deployment has nobody doing: those chains run the contracts without the
#: indexing behind volume and base APR. Showing "0.00%" there would be
#: reporting a measurement that was never taken, so the columns come out
#: entirely -- see `curve.lite`.
UNMEASURED_ON_LITE = ("volume", "base")


def visible_columns(columns, lite: bool) -> tuple[str, ...]:
    """The layout's columns, minus any the chain has no data for."""
    return tuple(c for c in columns if not (lite and c in UNMEASURED_ON_LITE))


COLUMN_CONTENT = {
    "base": lambda p: ft.Text(percent(p.base_apr), size=BODY, text_align=ft.TextAlign.RIGHT),
    "incentives": lambda p: ft.Column(
        reward_lines(p), spacing=0, horizontal_alignment=ft.CrossAxisAlignment.END
    ),
    "volume": lambda p: ft.Text(compact_usd(p.volume_24h), size=BODY),
    "tvl": lambda p: ft.Text(compact_usd(p.tvl), size=BODY),
}

#: Start loading the next page this many pixels before the end. Roughly two
#: screens, so the rows are usually there before the user reaches them.
SCROLL_THRESHOLD = 1200

#: How long to sit on a keystroke before asking the server. Long enough that
#: typing "steth" is one request rather than five.
SEARCH_DEBOUNCE = 0.35

#: The corner on the two controls above the list. Shared, because on a
#: phone they are the only two things on that row and a text field rounded
#: one way beside a dropdown rounded another reads as two kinds of thing
#: rather than as a pair.
FIELD_RADIUS = 8

#: And the height, for the same reason. Dense means different things to
#: the two -- the text field came out 40px against the dropdown's 48, and
#: side by side on a phone that reads as a mistake rather than as a pair.
#: The dropdown is the one that cannot be moved: it ignores `height`
#: outright and will not go below Material's 48px minimum however small
#: its padding. So the text field grows to meet it -- and it ignores
#: `height` too, so the growing is done with padding, measured rather
#: than derived: 14 gave 44px and each step is worth two.
FIELD_HEIGHT = 48
FIELD_INSET = 16


#: Reward marks in the list. Smaller than the pool stack beside the name:
#: this is which token, not which pool, and at list density a mark that
#: matches the text's own height reads as punctuation rather than as
#: another logo competing with the one on the left.
REWARD_MARK = 14


def reward_line(
    text: str,
    address: str,
    symbol: str,
    chain: str,
    *,
    muted: bool = False,
    tooltip: str = "",
) -> ft.Control:
    """One reward, marked with its token where the address is known.

    Shared with the portfolio, which shows the same thing about the same
    gauges -- one line per token, the mark beside the rate -- and would
    otherwise have grown its own not-quite-matching version.

    The row stays right-aligned like the rest of the column, so the mark
    sits between the numbers and the symbol rather than at a ragged left
    edge. A reward whose address the API did not carry -- merkle
    campaigns have none, and Lite chains sometimes omit them -- falls
    back to text alone rather than to an empty box.
    """
    label = ft.Text(
        text,
        size=SMALL,
        color=ft.Colors.ON_SURFACE_VARIANT if muted else None,
        text_align=ft.TextAlign.RIGHT,
        tooltip=tooltip or None,
    )
    if not address:
        return label
    coin = Coin(address=address, symbol=symbol, decimals=18)
    return ft.Row(
        [token_mark(coin, chain, REWARD_MARK), label],
        spacing=5,
        tight=True,
        alignment=ft.MainAxisAlignment.END,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        tooltip=tooltip or None,
    )


#: The icon on a line that has no rate. Points cannot be priced, so there
#: is nothing to put in the percentage's place -- and a blank where every
#: other line has a number reads as a missing value rather than as a
#: different kind of reward. A mark says which kind.
POINTS_ICON = ft.Icons.AUTO_AWESOME


def point_line(text: str, tooltip: str = "") -> ft.Control:
    """A reward with no rate: the points mark, then who is paying.

    Right-aligned like every other line in the column, and muted like the
    incentive lines, so the eye still runs down the numbers -- this one
    just does not have one.
    """
    return ft.Row(
        [
            ft.Icon(POINTS_ICON, size=REWARD_MARK, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(
                text,
                size=SMALL,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.RIGHT,
            ),
        ],
        spacing=5,
        tight=True,
        alignment=ft.MainAxisAlignment.END,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        tooltip=tooltip or None,
    )


def _token_note(token, plain: str) -> str:
    """A reward's tooltip, naming the wrapper where there is one.

    The row says crvUSD because crvUSD is what arrives; this says where
    the accounting on Merkl's own page will say ybwcrvUSD instead, so the
    two can be reconciled by whoever goes and looks.
    """
    if not token.wrapped:
        return f"{token.symbol}, {plain}"
    return (
        f"{token.paid_symbol}, {plain} as {token.symbol} -- a Merkl wrapper "
        f"that delivers {token.paid_symbol} when you claim."
    )


def campaign_lines(pool: Pool) -> list[ft.Control]:
    """What Merkl and the external campaigns pay, under the gauge's rewards.

    Three things Curve's own fields cannot say, in the order they matter:

      * **which token** the merkle campaign pays. `merkle_apr` is a bare
        percentage with no symbol attached to it;
      * **that there are two campaigns**, one for staked liquidity and one
        for unstaked. Only the higher is printed here -- the column has no
        room to explain the difference and they are usually within a
        rounding error of each other -- and the pool page breaks them out;
      * **points**, which have no price and so appear in no APR anywhere.

    The token named is the one that **arrives**, which on a wrapped
    campaign is not the one the campaign is denominated in: the
    pyUSD/crvUSD pool is quoted in `ybwcrvUSD` and pays crvUSD. See
    `curve.merkl.MerklToken`. The wrapper is not lost -- it is in the
    tooltip, and spelled out on the pool page -- but a symbol nobody
    recognises is the wrong thing to lead a row with.

    The `merkle_apr` line survives as the fallback for a chain Merkl does
    not cover or a request that did not come back, where a percentage with
    no token beats saying nothing.
    """
    lines: list[ft.Control] = []
    for token in pool.merkl.tokens:
        if token.points:
            lines.append(
                point_line(token.paid_symbol, _token_note(token, "paid through Merkl"))
            )
            continue
        unstaked, staked = pool.merkl.rate_for(token)
        lines.append(
            reward_line(
                f"{percent(max(unstaked, staked))} {token.paid_symbol}",
                token.paid_address,
                token.paid_symbol,
                pool.chain,
                muted=True,
                tooltip=_token_note(token, "paid through Merkl"),
            )
        )
    if not pool.merkl and pool.merkle_apr > 0:
        lines.append(
            ft.Text(
                f"{percent(pool.merkle_apr)} merkle",
                size=SMALL,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.RIGHT,
            )
        )
    lines += [
        point_line(campaign.label, campaign.describe()) for campaign in pool.points
    ]
    return lines


def reward_lines(pool: Pool) -> list[ft.Control]:
    """CRV first, then each incentive token on its own line.

    The same shape Curve uses, and it keeps a pool with three reward tokens
    from squeezing the other columns.
    """
    lines: list[ft.Control] = []
    if pool.crv_apr[1] > 0:
        lines.append(
            reward_line(
                f"{apr_range(*pool.crv_apr)} CRV",
                crv_token(pool),
                "CRV",
                pool.chain,
                muted=False,
            )
        )
    for incentive in pool.incentives:
        lines.append(
            reward_line(
                f"{percent(incentive.apr)} {incentive.symbol}",
                incentive.token_address,
                incentive.symbol,
                pool.chain,
                muted=True,
            )
        )
    lines += campaign_lines(pool)
    if not lines:
        lines.append(
            # An en dash, not a hyphen: it is standing in for a missing
            # number in a column of numbers, which is what it is for.
            ft.Text(
                "–",  # noqa: RUF001 -- an en dash, standing in for a missing number
                size=BODY,
                color=ft.Colors.OUTLINE,
                text_align=ft.TextAlign.RIGHT,
            )
        )
    return lines


def _name_cell(pool: Pool, logo_size: float = MARK_SIZE) -> ft.Control:
    """Overlapping coin logos, then the pool's name and its assets."""
    return ft.Row(
        [
            pool_stack(pool, size=logo_size),
            ft.Column(
                [
                    ft.Text(pool.display_name, size=ROW_TITLE, weight=ft.FontWeight.W_500),
                    ft.Text(
                        " ".join(pool.coin_symbols),
                        size=SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                ],
                spacing=1,
                expand=True,
            ),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        expand=True,
    )


def _metric(label: str, value: str) -> ft.Control:
    """A labelled figure, for the card layout where there are no headers."""
    return ft.Row(
        [
            ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(value, size=SMALL),
        ],
        spacing=4,
        tight=True,
    )


class PoolRow(ft.Container):
    """One pool. Click anywhere to open it."""

    def __init__(
        self,
        pool: Pool,
        on_open: Callable[[Pool], None],
        index: int = 0,
        layout: Layout | None = None,
    ) -> None:
        self.pool = pool
        layout = layout or layout_for(2000.0)
        content = self._card(pool) if layout.cards else self._row(pool, layout)
        super().__init__(
            content=content,
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            on_click=lambda _e: on_open(pool),
            # The hover colour is the theme's business, not this control's
            # -- a keyed control is frozen after a rebuild and cannot be
            # assigned to at all. See `theme.rows_theme`.
            ink=True,
            # Position-based rather than address-based so a UI test can
            # always reach "the first row" without knowing the data.
            key=f"pool-row-{index}",
        )

    def _row(self, pool: Pool, layout: Layout) -> ft.Control:
        cells: list[ft.Control] = [_name_cell(pool)]
        for column in visible_columns(layout.columns, pool.lite):
            cells.append(
                ft.Container(
                    COLUMN_CONTENT[column](pool),
                    width=COLUMN_WIDTH[column],
                    alignment=ft.Alignment.CENTER_RIGHT,
                )
            )
        return ft.Row(cells, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _card(self, pool: Pool) -> ft.Control:
        """Two lines instead of five columns.

        Below ~760px a five-column table is unreadable however the widths
        are juggled, so the row becomes a card: identity on the first line,
        the figures that decide a pool underneath, each with its own label
        since there are no column headers to read them against.
        """
        return ft.Column(
            [
                ft.Row(
                    [
                        _name_cell(pool),
                        # On a Lite chain the headline is TVL, because
                        # there is no volume to lead with.
                        ft.Column(
                            [
                                ft.Text(
                                    compact_usd(pool.tvl if pool.lite else pool.volume_24h),
                                    size=BODY,
                                ),
                                ft.Text(
                                    "TVL" if pool.lite else f"{compact_usd(pool.tvl)} TVL",
                                    size=LABEL,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                            spacing=0,
                            horizontal_alignment=ft.CrossAxisAlignment.END,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                ft.Row(
                    [
                        *([] if pool.lite else [_metric("base", percent(pool.base_apr))]),
                        *(
                            [_metric("rewards", f"{apr_range(*pool.crv_apr)} CRV")]
                            if pool.crv_apr[1] > 0
                            else []
                        ),
                        *[
                            _metric(i.symbol, percent(i.apr))
                            for i in pool.incentives[:2]
                        ],
                        # The same two-per-kind cap the incentives take: a
                        # card is two lines and a pool paying five things
                        # would make it six. The pool page shows them all.
                        *[
                            _metric(t.symbol, percent(max(pool.merkl.rate_for(t))))
                            for t in pool.merkl.tokens[:2]
                            if not t.points
                        ],
                        *[
                            point_line(t.symbol, f"{t.symbol}, paid through Merkl")
                            for t in pool.merkl.points[:2]
                        ],
                        *[
                            point_line(c.label, c.describe())
                            for c in pool.points[:2]
                        ],
                    ],
                    spacing=14,
                    wrap=True,
                ),
            ],
            spacing=6,
        )


class PoolListView(ft.Column):
    """Search box, sortable header, and a lazily-paged list of rows."""

    def __init__(self, page: ft.Page, on_open: Callable[[Pool], None]) -> None:
        #: Whether the attached feed is a Curve Lite chain, which decides
        #: what there is to show. Set before anything builds a header.
        self._lite = False
        # `ft.Column` exposes `page` as a read-only property that raises
        # until the control is mounted, so the reference used for
        # `run_task` needs a name -- and a scroll handler must be able to
        # schedule work the moment it fires.
        self._page = page
        self._on_open = on_open
        self.feed: PoolFeed | None = None
        self._sort = DEFAULT_SORT
        self._search_token = 0
        self._layout = layout_for(2000.0)

        self.search = ft.TextField(
            key="pool-search",
            hint_text="Search name, symbol or paste an address",
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._search_changed,
            dense=True,
            border_radius=FIELD_RADIUS,
            # Height is set by the padding, not by `height`: neither of
            # these two controls honours that at all. See FIELD_HEIGHT.
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=FIELD_INSET),
        )
        self.count_label = ft.Text(
            "", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT, key="pool-count"
        )
        # Cards have no column headers to click, so narrow layouts sort
        # through this instead. Hidden while the table is showing.
        self.sort_picker = ft.Dropdown(
            key="pool-sort",
            options=[ft.DropdownOption(key=o.key, text=o.label) for o in SORTS],
            value=self._sort,
            width=140,
            dense=True,
            # The same corners as the search box beside it. The height
            # is matched from that side -- see FIELD_HEIGHT.
            border_radius=FIELD_RADIUS,
            visible=False,
            on_select=self._sort_picked,
        )
        # A plain Column, not a `ListView`: the window is what scrolls
        # now (see `CurveApp.body`), and a list that scrolls inside an
        # unbounded parent is a Flutter layout error rather than a second
        # scrollbar. The cost is that every loaded row is built rather than
        # only the visible ones -- a page is 50 rows and the longest chain
        # is a few hundred.
        self.rows = ft.Column(key="pool-rows", spacing=0)
        self.footer = ft.Container(
            ft.Row(
                [ft.ProgressRing(width=16, height=16), ft.Text("Loading…", size=SMALL)],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            padding=12,
            visible=False,
        )
        self._header = self._build_header()

        # Wraps the rows so a theme can be scoped to them: `ListView` takes
        # none, and the headings above are not rows and should not take the
        # rows' hover colour.
        self._rows_box = ft.Container(self.rows, theme=theme.rows_theme(page))

        # The table is a panel: white, with the rules between its rows.
        # Explicit rather than inherited, because the Chad theme puts a
        # grey page behind it and the table must stay on white -- in the
        # Material themes `SURFACE` is the page colour anyway, so this
        # changes nothing there.
        self._table = ft.Container(
            ft.Column([self._header, self._rows_box], spacing=0),
            bgcolor=ft.Colors.SURFACE,
            # Outlined under Chad, which is a theme of bordered boxes and
            # needs an edge for its shadow to come from; left alone in
            # light and dark, where tone already separates the table from
            # the page. Clipped either way, because the heading band and
            # the rows' own rules are square and would otherwise cross the
            # rounded corners.
            border=theme.panel_border(page),
            border_radius=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            shadow=theme.panel_shadow(page),
        )
        super().__init__(
            controls=[
                ft.Row(
                    [
                        ft.Container(self.search, expand=True),
                        self.sort_picker,
                        self.count_label,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=16,
                ),
                self._table,
                self.footer,
            ],
            spacing=10,
        )

    # -- header -----------------------------------------------------------

    def _build_header(self) -> ft.Container:
        """Column headings, each one a click target that re-sorts the list.

        These are plain `Container`s with `on_click`, not `TextButton`s.
        A TextButton here hovered correctly but never fired its handler in
        the published web build -- no exception, the event simply never
        arrived -- while the identical pattern on `PoolRow` worked. The
        container also gives the whole cell as a hit target, which is the
        better affordance anyway.
        """
        self._sort_cells: dict[str, ft.Container] = {}
        widths = COLUMN_WIDTH
        cells: list[ft.Control] = [
            ft.Container(
                ft.Text("Pool", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT), expand=True
            )
        ]
        # Ordered to match the row layout, not the SORTS tuple.
        for key in ("base", "incentives", "volume", "tvl"):
            cell = ft.Container(
                width=widths[key],
                alignment=ft.Alignment.CENTER_RIGHT,
                padding=ft.Padding.symmetric(horizontal=6, vertical=8),
                on_click=lambda _e, k=key: self._sort_by(k),
                ink=True,
                border_radius=6,
            )
            self._sort_cells[key] = cell
            cells.append(cell)
        self._sync_header()
        return ft.Container(
            ft.Row(cells, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=16, vertical=2),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE)),
            # A grey band above the rows, where the theme asks for one:
            # `thead` takes the table's border colour on linux.org.ru.
            # None everywhere else, and a Material table has no band.
            bgcolor=theme.header_bg(self._page),
        )

    def _sync_header(self) -> None:
        """Mark the active column. Sorting is always descending.

        The arrow is a Material icon rather than a "↓" in the label: the web
        build's font has no glyph for it and renders a tofu box, whereas the
        icon font is bundled and works on both platforms.
        """
        showing = visible_columns(self._layout.columns, self._lite)
        for key, cell in self._sort_cells.items():
            cell.visible = key in showing
            option = next(o for o in SORTS if o.key == key)
            active = key == self._sort
            label = ft.Text(
                option.label,
                size=SMALL,
                weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL,
                color=ft.Colors.PRIMARY if active else ft.Colors.ON_SURFACE_VARIANT,
            )
            cell.content = (
                ft.Row(
                    [label, ft.Icon(ft.Icons.ARROW_DOWNWARD, size=BODY, color=ft.Colors.PRIMARY)],
                    spacing=2,
                    tight=True,
                    alignment=ft.MainAxisAlignment.END,
                )
                if active
                else ft.Row([label], tight=True, alignment=ft.MainAxisAlignment.END)
            )

    # -- feed -------------------------------------------------------------

    def set_layout(self, layout: Layout) -> None:
        """Adopt a new layout, rebuilding the rows only if it changed."""
        if layout == self._layout:
            return
        self._layout = layout
        self._header.visible = layout.shows_column_headers
        self.sort_picker.visible = not layout.shows_column_headers
        # The count is the first thing to go on a phone: that row already
        # carries the search box and the sort picker, and how many pools a
        # chain has is not what anybody is there to read.
        self.count_label.visible = not layout.cards
        self.sort_picker.value = self._sort
        self._sync_header()
        self._rebuild_rows()
        safe_update(self)

    def _rebuild_rows(self) -> None:
        """Re-render the rows already loaded, in the current layout."""
        pools = [row.pool for row in self.rows.controls if isinstance(row, PoolRow)]
        self.rows.controls = [
            PoolRow(p, self._on_open, i, self._layout) for i, p in enumerate(pools)
        ]

    def rebuild(self) -> None:
        """Take on a theme that changed.

        Colour repaints itself; what does not is anything decided when a
        control was built -- the panel's shadow, the band behind the
        headings, and the rows' hover colour. All three live on containers
        that outlive the theme change, so the rows themselves are left
        alone: re-making them is what freezes them.
        """
        self._table.shadow = theme.panel_shadow(self._page)
        self._table.border = theme.panel_border(self._page)
        self._header.bgcolor = theme.header_bg(self._page)
        self._rows_box.theme = theme.rows_theme(self._page)
        self._sync_header()

    def attach(self, feed: PoolFeed) -> None:
        """Point the view at a (new) feed, e.g. after a chain change."""
        self.feed = feed
        self._lite = feed.lite
        # A Lite chain has no volume to sort by, so the feed opens on TVL
        # and the header has to agree with it.
        self._sort = feed.sort_by or DEFAULT_SORT
        self.sort_picker.value = self._sort
        self.sort_picker.options = [
            ft.DropdownOption(key=o.key, text=o.label)
            for o in SORTS
            if not (self._lite and o.key in UNMEASURED_ON_LITE)
        ]
        self.search.value = ""
        self._sync_header()
        self.rows.controls = []
        self._sync_count()

    def _sort_picked(self, _e: AnyEvent) -> None:
        """The phone's dropdown. A named method rather than a lambda: the
        lambda read `self.sort_picker` from inside the statement defining
        it, which works but leaves its own type unknowable."""
        self._sort_by(self.sort_picker.value or DEFAULT_SORT)

    def _sort_by(self, key: str) -> None:
        if self.feed is None or key == self._sort:
            return
        self._sort = key
        self.sort_picker.value = key
        self._sync_header()
        from curve.sort import sort_field  # local: avoids a cycle at import

        self.feed.reset(sort_by=sort_field(key))
        self.rows.controls = []
        safe_update(self)
        self._run(self.load_more)

    def _search_changed(self, e: AnyEvent) -> None:
        self._run(self._debounced_search, e.control.value or "")

    async def _debounced_search(self, query: str) -> None:
        """Wait out the typing, then ask the server.

        The token check is what makes this a debounce rather than a delay:
        every keystroke starts a new coroutine, and all but the last find
        themselves superseded when they wake.
        """
        self._search_token += 1
        token = self._search_token
        await asyncio.sleep(SEARCH_DEBOUNCE)
        if token != self._search_token or self.feed is None:
            return
        self.feed.reset(search=query.strip())
        self.rows.controls = []
        safe_update(self)
        await self.load_more()

    def page_scrolled(self, e: ft.OnScrollEvent) -> None:
        """Pull the next page when the end comes into view.

        Called by the app rather than by a scrollable of this view's own:
        the scroller is the window, and it is shared with every other page.
        """
        if self.feed is None or self.feed.loading or self.feed.exhausted:
            return
        if e.max_scroll_extent - e.pixels > SCROLL_THRESHOLD:
            return
        self._run(self.load_more)

    async def load_more(self) -> None:
        """Fetch and append the next page, if there is one."""
        feed = self.feed
        if feed is None or feed.loading or feed.exhausted:
            return
        self.footer.visible = True
        safe_update(self.footer)

        new_pools = await feed.load_more()

        if feed is not self.feed:  # chain changed while we waited
            return
        start = len(self.rows.controls)
        self.rows.controls.extend(
            PoolRow(p, self._on_open, start + offset, self._layout)
            for offset, p in enumerate(new_pools)
        )
        self.footer.visible = False
        self._sync_count()
        safe_update(self)

    def _sync_count(self) -> None:
        feed = self.feed
        if feed is None:
            self.count_label.value = ""
            return
        if feed.error:
            self.count_label.value = feed.error
        elif feed.total is None:
            self.count_label.value = "Loading…"
        else:
            # How many there are, not how many have been fetched. "50 of
            # 387" answered a question about the paging rather than about
            # the chain, and it changed under you as you scrolled.
            self.count_label.value = f"{feed.total} pools"

    def _run(self, handler, *args) -> None:
        """Schedule an async handler.

        `run_task` insists on a coroutine *function*: it rejects a bare
        coroutine object with a TypeError, which is why every call site
        passes the method and its arguments separately.
        """
        self._page.run_task(handler, *args)
