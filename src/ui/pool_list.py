"""The pool list: search, sortable columns, one row per pool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import flet as ft

from curve.api import PoolFeed
from curve.format import apr_range, compact_usd, percent
from curve.models import Coin, Pool
from curve.rewards import crv_token
from curve.sort import BASE_WINDOWS, DEFAULT_BASE_WINDOW, DEFAULT_SORT, SORTS

from . import AnyEvent, safe_update, theme
from .logos import MARK_SIZE, pool_stack, token_mark
from .responsive import Layout, layout_for
from .typography import BODY, LABEL, ROW_TITLE, SMALL

#: Column widths, shared by the header and every row so they line up.
#: Base is the widest of the three figures because its heading carries the
#: window picker beside it: "Base APY 7d v" with the sort arrow needs 150,
#: and clips the arrow at 110.  The 40px comes out of the Pool column,
#: which expands.
W_BASE = 150
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
#: indexing behind volume and base APR.
UNMEASURED_ON_LITE = ("volume", "base")


def visible_columns(columns, lite: bool) -> tuple[str, ...]:
    """The layout's columns, minus any the chain has no data for."""
    return tuple(c for c in columns if not (lite and c in UNMEASURED_ON_LITE))


#: One cell each, given the pool and the window the Base APY column is
#: being read over.  Only that column has a window; the rest take it and
#: ignore it so `_row` can call them all the same way.
COLUMN_CONTENT = {
    "base": lambda p, w: ft.Text(
        percent(p.base_for(w)), size=BODY, text_align=ft.TextAlign.RIGHT
    ),
    "incentives": lambda p, _w: ft.Column(
        reward_lines(p), spacing=0, horizontal_alignment=ft.CrossAxisAlignment.END
    ),
    "volume": lambda p, _w: ft.Text(compact_usd(p.volume_24h), size=BODY),
    "tvl": lambda p, _w: ft.Text(compact_usd(p.tvl), size=BODY),
}

#: Start loading the next page this many pixels before the end.

#: How long to sit on a keystroke before asking the server.
SEARCH_DEBOUNCE = 0.35

#: A column heading's own padding.  The band used to get 2px of it from the
#: row around every cell; it is all in here now, so that the Base APY
#: window's block can span the band rather than stop 2px short of it.
HEAD_PAD = ft.Padding.symmetric(horizontal=6, vertical=10)

#: The corner on the two controls above the list.
FIELD_RADIUS = 8

#: The cross that empties the search box. Sized to sit inside the field
#: rather than to be tapped from across the room, but the 32px box keeps
#: it above the 24px a thumb needs to land on reliably.
CLEAR_MARK = 18
CLEAR_BOX = 32

#: And the height, for the same reason. Dense means different things to the
#: two -- the text field came out 40px against the dropdown's 48, and side
#: by side on a phone that reads as a mistake rather than as a pair.
FIELD_HEIGHT = 48
FIELD_INSET = 16


#: Reward marks in the list. Smaller than the pool stack beside the name:
#: this is which token, not which pool, and at list density a mark that
#: matches the text's own height reads as punctuation rather than as another
#: logo competing with the one on the left.
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
    """One reward, marked with its token where the address is known."""
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


#: The icon on a line that has no rate. Points cannot be priced, so there is
#: nothing to put in the percentage's place -- and a blank where every other
#: line has a number reads as a missing value rather than as a different
#: kind of reward.
POINTS_ICON = ft.Icons.AUTO_AWESOME


def point_line(text: str, tooltip: str = "") -> ft.Control:
    """A reward with no rate: the points mark, then who is paying."""
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
    """A reward's tooltip, naming the wrapper where there is one."""
    if not token.wrapped:
        return f"{token.symbol}, {plain}"
    return (
        f"{token.paid_symbol}, {plain} as {token.symbol} -- a Merkl wrapper "
        f"that delivers {token.paid_symbol} when you claim."
    )


def campaign_lines(pool: Pool) -> list[ft.Control]:
    """What Merkl and the external campaigns pay, under the gauge's rewards."""
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
    """CRV first, then each incentive token on its own line."""
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
        base_window: str = DEFAULT_BASE_WINDOW,
    ) -> None:
        self.pool = pool
        self.base_window = base_window
        layout = layout or layout_for(2000.0)
        content = self._card(pool) if layout.cards else self._row(pool, layout)
        super().__init__(
            content=content,
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            on_click=lambda _e: on_open(pool),
            ink=True,
            key=f"pool-row-{index}",
        )

    def _row(self, pool: Pool, layout: Layout) -> ft.Control:
        cells: list[ft.Control] = [_name_cell(pool)]
        for column in visible_columns(layout.columns, pool.lite):
            cells.append(
                ft.Container(
                    COLUMN_CONTENT[column](pool, self.base_window),
                    width=COLUMN_WIDTH[column],
                    alignment=ft.Alignment.CENTER_RIGHT,
                )
            )
        return ft.Row(cells, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _card(self, pool: Pool) -> ft.Control:
        """Two lines instead of five columns."""
        return ft.Column(
            [
                ft.Row(
                    [
                        _name_cell(pool),
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
                        *(
                            []
                            if pool.lite
                            else [
                                _metric(
                                    "base", percent(pool.base_for(self.base_window))
                                )
                            ]
                        ),
                        *(
                            [_metric("rewards", f"{apr_range(*pool.crv_apr)} CRV")]
                            if pool.crv_apr[1] > 0
                            else []
                        ),
                        *[
                            _metric(i.symbol, percent(i.apr))
                            for i in pool.incentives[:2]
                        ],
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
        #: Whether the attached feed is a Curve Lite chain, which
        #: decides what there is to show.
        self._lite = False
        self._page = page
        self._on_open = on_open
        self.feed: PoolFeed | None = None
        self._sort = DEFAULT_SORT
        #: Which window the Base APY column draws.  A session's choice: the
        #: feed has to know it too, because the server ranks by the field
        #: that matches.
        self._base_window = DEFAULT_BASE_WINDOW
        self._search_token = 0
        #: Held across an append. `feed.loading` is not enough on its own:
        #: a sort the server cannot do has the whole chain in memory by the
        #: second page, so `load_more` returns without ever going near the
        #: network and never sets it. See `page_scrolled`.
        self._appending = False
        #: How tall the list has to be before another page is worth asking
        #: for: what it was when the last one was taken, plus a screenful.
        #: See `page_scrolled`.
        self._grown_to = float("-inf")
        self._layout = layout_for(2000.0)

        #: Shown only once there is something to clear: an empty box with
        #: a cross in it invites a tap that would do nothing.
        self.clear_search = ft.IconButton(
            ft.Icons.CLOSE,
            key="pool-search-clear",
            tooltip="Clear the search",
            icon_size=CLEAR_MARK,
            icon_color=ft.Colors.ON_SURFACE_VARIANT,
            padding=ft.Padding.all(0),
            size_constraints=ft.BoxConstraints(
                max_width=CLEAR_BOX, max_height=CLEAR_BOX
            ),
            visible=False,
            on_click=self._search_cleared,
        )
        self.search = ft.TextField(
            key="pool-search",
            hint_text="Search name, symbol or paste an address",
            prefix_icon=ft.Icons.SEARCH,
            suffix_icon=self.clear_search,
            suffix_icon_size_constraints=ft.BoxConstraints(
                min_width=CLEAR_BOX, min_height=CLEAR_BOX
            ),
            on_change=self._search_changed,
            dense=True,
            border_radius=FIELD_RADIUS,
            content_padding=ft.Padding.symmetric(horizontal=12, vertical=FIELD_INSET),
        )
        self.count_label = ft.Text(
            "", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT, key="pool-count"
        )
        self.sort_picker = ft.Dropdown(
            key="pool-sort",
            options=[ft.DropdownOption(key=o.key, text=o.label) for o in SORTS],
            value=self._sort,
            width=140,
            dense=True,
            border_radius=FIELD_RADIUS,
            visible=False,
            on_select=self._sort_picked,
        )
        #: The cards layout's stand-in for the menu in the heading, which is
        #: where this lives everywhere there are headings at all -- the same
        #: arrangement as `sort_picker` beside it, for the same reason.
        self.base_picker = ft.Dropdown(
            key="pool-base-window",
            options=[
                ft.DropdownOption(key=w, text=f"Base {w}") for w in BASE_WINDOWS
            ],
            value=self._base_window,
            # The sort picker's width. Narrower clips: 120 drew "Base 7".
            width=140,
            dense=True,
            border_radius=FIELD_RADIUS,
            visible=False,
            on_select=self._base_window_picked,
        )
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

        self._rows_box = ft.Container(self.rows, theme=theme.rows_theme(page))

        self._table = ft.Container(
            ft.Column([self._header, self._rows_box], spacing=0),
            bgcolor=ft.Colors.SURFACE,
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
                        self.base_picker,
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
        """Column headings, each one a click target that re-sorts the list."""
        self._sort_cells: dict[str, ft.Container] = {}
        widths = COLUMN_WIDTH
        cells: list[ft.Control] = [
            ft.Container(
                ft.Text("Pool", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT), expand=True
            )
        ]
        for key in ("base", "incentives", "volume", "tvl"):
            # Base is two halves with a tap each, so the whole-cell tap and
            # the padding that would sit above its divider both move inwards.
            split = key == "base"
            cell = ft.Container(
                width=widths[key],
                alignment=ft.Alignment.CENTER_RIGHT,
                padding=None if split else HEAD_PAD,
                on_click=None if split else (lambda _e, k=key: self._sort_by(k)),
                ink=not split,
                border_radius=6,
            )
            self._sort_cells[key] = cell
            cells.append(cell)
        self._sync_header()
        return ft.Container(
            ft.Row(cells, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE)),
            bgcolor=theme.header_bg(self._page),
        )

    def _window_menu(self) -> ft.Control:
        """The right half of the Base APY heading: the window, and a menu.

        A menu rather than a `ft.Dropdown` because a dropdown is a 48px
        control: dropped into a header cell it fills the cell, leaving no
        room for the heading, and adds 29px to the band.  Measured, both.
        This block is the band's own height and adds nothing to it.
        """
        return ft.PopupMenuButton(
            key="base-window",
            content=ft.Container(
                ft.Row(
                    [
                        ft.Text(
                            self._base_window,
                            size=LABEL,
                            weight=ft.FontWeight.BOLD,
                            color=ft.Colors.PRIMARY,
                        ),
                        ft.Icon(
                            ft.Icons.ARROW_DROP_DOWN,
                            size=BODY,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    spacing=0,
                    tight=True,
                ),
                padding=ft.Padding.only(left=8, right=2, top=10, bottom=10),
                bgcolor=ft.Colors.with_opacity(0.07, ft.Colors.ON_SURFACE),
                border=ft.Border(left=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            ),
            items=[
                ft.PopupMenuItem(
                    content=ft.Text(window, size=SMALL),
                    checked=window == self._base_window,
                    on_click=lambda _e, w=window: self._base_window_to(w),
                )
                for window in BASE_WINDOWS
            ],
            padding=ft.Padding.all(0),
            tooltip="Base APY over one day or seven",
        )

    def _sync_header(self) -> None:
        """Mark the active column. Sorting is always descending."""
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
            named: list[ft.Control] = [label]
            if active:
                named.append(
                    ft.Icon(ft.Icons.ARROW_DOWNWARD, size=BODY, color=ft.Colors.PRIMARY)
                )
            heading = ft.Row(
                named, spacing=2, tight=True, alignment=ft.MainAxisAlignment.END
            )
            if key != "base":
                cell.content = heading
                continue
            # Two halves of one cell: the name sorts, the window opens a menu.
            cell.content = ft.Row(
                [
                    ft.Container(
                        heading,
                        padding=HEAD_PAD,
                        alignment=ft.Alignment.CENTER_RIGHT,
                        expand=True,
                        ink=True,
                        on_click=lambda _e: self._sort_by("base"),
                    ),
                    *([] if self._lite else [self._window_menu()]),
                ],
                spacing=0,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

    # -- feed -------------------------------------------------------------

    def _sync_base_picker(self) -> None:
        """The toolbar picker: only where the heading cannot carry the menu.

        Which is the cards layout, and not on a lite chain -- nobody is
        counting the trades a base APY comes from there, so there is no
        column and no card metric to read either way.
        """
        self.base_picker.value = self._base_window
        self.base_picker.visible = self._layout.cards and not self._lite

    def set_layout(self, layout: Layout) -> None:
        """Adopt a new layout, rebuilding the rows only if it changed."""
        if layout == self._layout:
            return
        self._layout = layout
        self._header.visible = layout.shows_column_headers
        self.sort_picker.visible = not layout.shows_column_headers
        self.count_label.visible = not layout.cards
        self.sort_picker.value = self._sort
        self._sync_base_picker()
        self._sync_header()
        self._rebuild_rows()
        safe_update(self)

    def refresh_figures(self, figures: dict[str, dict[str, float]]) -> int:
        """Take fresher TVL, volume and base APR onto the rows on screen.

        In place, and without reordering: the order is the server's, and a
        row jumping past its neighbour because its volume ticked over is
        worse than a list that is briefly ordered by figures a few minutes
        old. The next real load puts it right.
        """
        moved = 0
        for row in self.rows.controls:
            if not isinstance(row, PoolRow):
                continue
            figure = figures.get(row.pool.address.lower())
            if figure is not None and row.pool.take_figures(figure):
                moved += 1
        if moved:
            self._rebuild_rows()
            safe_update(self)
        return moved

    def _rebuild_rows(self) -> None:
        """Re-render the rows already loaded, in the current layout."""
        pools = [row.pool for row in self.rows.controls if isinstance(row, PoolRow)]
        self.rows.controls = [
            PoolRow(p, self._on_open, i, self._layout, self._base_window)
            for i, p in enumerate(pools)
        ]

    def rebuild(self) -> None:
        """Take on a theme that changed."""
        self._table.shadow = theme.panel_shadow(self._page)
        self._table.border = theme.panel_border(self._page)
        self._header.bgcolor = theme.header_bg(self._page)
        self._rows_box.theme = theme.rows_theme(self._page)
        self._sync_header()

    def attach(self, feed: PoolFeed) -> None:
        """Point the view at a (new) feed, e.g. after a chain change."""
        self.feed = feed
        self._grown_to = float("-inf")
        self._lite = feed.lite
        self._sort = feed.sort_by or DEFAULT_SORT
        self._base_window = feed.base_window
        self.sort_picker.value = self._sort
        self.sort_picker.options = [
            ft.DropdownOption(key=o.key, text=o.label)
            for o in SORTS
            if not (self._lite and o.key in UNMEASURED_ON_LITE)
        ]
        self._sync_base_picker()
        self.search.value = ""
        self.clear_search.visible = False
        self._sync_header()
        self.rows.controls = []
        self._sync_count()

    def _sort_picked(self, _e: AnyEvent) -> None:
        """The phone's dropdown. A named method rather than a lambda: the
        lambda read `self.sort_picker` from inside the statement
        defining it, which works but leaves its own type unknowable.
        """
        self._sort_by(self.sort_picker.value or DEFAULT_SORT)

    def _sort_by(self, key: str) -> None:
        if self.feed is None or key == self._sort:
            return
        self._sort = key
        self.sort_picker.value = key
        self._sync_header()
        # The column, not the server's field for it: the feed hands this to
        # `list_pools`, which needs the column to sort a lite chain locally and
        # translates it for the wire itself.
        self.feed.reset(sort_by=key)
        self._grown_to = float("-inf")
        self.rows.controls = []
        safe_update(self)
        self._run(self.load_more)

    def _base_window_picked(self, _e: AnyEvent) -> None:
        """The toolbar dropdown. Named for the same reason `_sort_picked` is."""
        self._base_window_to(self.base_picker.value or DEFAULT_BASE_WINDOW)

    def _base_window_to(self, window: str) -> None:
        """Read the Base APY column over `window`.

        Both figures came down with every row, so when the list is ordered by
        something else there is nothing to fetch: redraw and stop.  Ordered by
        Base APY, the server's order is the one that changes, and only a reload
        can produce it.
        """
        if self.feed is None or window == self._base_window:
            return
        self._base_window = window
        self.base_picker.value = window
        self.feed.base_window = window
        self._sync_header()
        if self._sort != "base":
            self._rebuild_rows()
            safe_update(self)
            return
        self.feed.reset(base_window=window)
        self._grown_to = float("-inf")
        self.rows.controls = []
        safe_update(self)
        self._run(self.load_more)

    def _search_changed(self, e: AnyEvent) -> None:
        query = e.control.value or ""
        self.clear_search.visible = bool(query)
        safe_update(self.clear_search)
        self._run(self._debounced_search, query, self._claim_search())

    def _search_cleared(self, _e: AnyEvent) -> None:
        """The cross. Empties the box and asks for the whole list back at
        once: the debounce is there to wait out typing, and this is a tap
        that has already finished.
        """
        self.search.value = ""
        self.clear_search.visible = False
        safe_update(self.search)
        self._claim_search()
        if self.feed is not None:
            self.feed.reset(search="")
            self._grown_to = float("-inf")
            self.rows.controls = []
            safe_update(self)
            self._run(self.load_more)

    def _claim_search(self) -> int:
        """Take the latest search over, so anything still waiting out its
        debounce drops on the floor. Claimed as the event arrives, not
        when its task starts: a task that has not been given a slice yet
        would otherwise claim the search after a later tap already had.
        """
        self._search_token += 1
        return self._search_token

    async def _debounced_search(self, query: str, token: int) -> None:
        """Wait out the typing, then ask the server."""
        await asyncio.sleep(SEARCH_DEBOUNCE)
        if token != self._search_token or self.feed is None:
            return
        self.feed.reset(search=query.strip())
        self._grown_to = float("-inf")
        self.rows.controls = []
        safe_update(self)
        await self.load_more()

    def page_scrolled(self, e: ft.OnScrollEvent) -> None:
        """Pull the next page when less than a screen is left below, and not
        again until that page has arrived.

        Both halves are the list's own measurements, which is the point.  A
        page is asked for *because* there is little left below -- and until
        the rows land that stays true, so every event asks again.  A gesture
        is forty events, the pages come from memory under a sort the server
        cannot rank, and a flick down Ethereum took the whole chain at a
        hundred percent of a core.

        The page has arrived when the list has grown by a screenful.  A page
        is fifty rows and a screen is a dozen, so it is satisfied when they
        are really there and not before -- and it needs no clock, which would
        only be a guess about how long a frame takes on someone else's
        machine.  Growth in stages is what defeated measuring it as "taller
        at all": laying out one row of the fifty was enough, and the extent
        went 2,601 to 2,654 before it went to 5,868.

        `viewport_dimension` for the trigger as well, rather than a fixed
        1,200 px, so "nearly the end" means the same on a phone as on a
        desktop.
        """
        if self.feed is None or self.feed.loading or self.feed.exhausted:
            return
        if self._appending:
            return
        if e.extent_total < self._grown_to:
            return
        if e.extent_after > e.viewport_dimension:
            return
        self._grown_to = e.extent_total + e.viewport_dimension
        self._run(self.load_more)

    async def load_more(self) -> None:
        """Fetch and append the next page, if there is one."""
        feed = self.feed
        if feed is None or feed.loading or feed.exhausted or self._appending:
            return
        self._appending = True
        try:
            self.footer.visible = True
            safe_update(self.footer)

            new_pools = await feed.load_more()

            if feed is not self.feed:  # chain changed while we waited
                return
            start = len(self.rows.controls)
            self.rows.controls.extend(
                PoolRow(p, self._on_open, start + offset, self._layout, self._base_window)
                for offset, p in enumerate(new_pools)
            )
            self.footer.visible = False
            self._sync_count()
            safe_update(self)
            # Let the fifty just handed over be built before another scroll
            # event can ask for fifty more.  Without it the guard is released
            # in the same slice the rows were queued in, and the burst is
            # back.
            await asyncio.sleep(0)
        finally:
            self._appending = False

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
            self.count_label.value = f"{feed.total} pools"

    def _run(self, handler, *args) -> None:
        """Schedule an async handler."""
        self._page.run_task(handler, *args)
