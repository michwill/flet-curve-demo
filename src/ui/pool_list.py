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
from typing import Callable

import flet as ft

from curve.api import PoolFeed
from curve.format import apr_range, compact_usd, percent
from curve.models import Pool
from curve.sort import DEFAULT_SORT, SORTS

from . import safe_update
from .logos import pool_stack
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


def reward_lines(pool: Pool) -> list[ft.Control]:
    """CRV first, then each incentive token on its own line.

    The same shape Curve uses, and it keeps a pool with three reward tokens
    from squeezing the other columns.
    """
    lines: list[ft.Control] = []
    if pool.crv_apr[1] > 0:
        lines.append(
            ft.Text(f"{apr_range(*pool.crv_apr)} CRV", size=SMALL, text_align=ft.TextAlign.RIGHT)
        )
    for incentive in pool.incentives:
        lines.append(
            ft.Text(
                f"{percent(incentive.apr)} {incentive.symbol}",
                size=SMALL,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.RIGHT,
            )
        )
    if pool.merkle_apr > 0:
        lines.append(
            ft.Text(
                f"{percent(pool.merkle_apr)} merkle",
                size=SMALL,
                color=ft.Colors.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.RIGHT,
            )
        )
    if not lines:
        lines.append(
            ft.Text("–", size=BODY, color=ft.Colors.OUTLINE, text_align=ft.TextAlign.RIGHT)
        )
    return lines


def _name_cell(pool: Pool, logo_size: float = 27) -> ft.Control:
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
            ink=True,
            # Position-based rather than address-based so a UI test can
            # always reach "the first row" without knowing the data.
            key=f"pool-row-{index}",
        )

    def _row(self, pool: Pool, layout: Layout) -> ft.Control:
        cells: list[ft.Control] = [_name_cell(pool)]
        for column in layout.columns:
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
                        ft.Column(
                            [
                                ft.Text(compact_usd(pool.volume_24h), size=BODY),
                                ft.Text(
                                    f"{compact_usd(pool.tvl)} TVL",
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
                        _metric("base", percent(pool.base_apr)),
                        *(
                            [_metric("rewards", f"{apr_range(*pool.crv_apr)} CRV")]
                            if pool.crv_apr[1] > 0
                            else []
                        ),
                        *[
                            _metric(i.symbol, percent(i.apr))
                            for i in pool.incentives[:2]
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
            border_radius=8,
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
            visible=False,
            on_select=lambda _e: self._sort_by(self.sort_picker.value or DEFAULT_SORT),
        )
        self.rows = ft.ListView(
            key="pool-rows",
            expand=True,
            spacing=0,
            on_scroll=self._scrolled,
            # Throttle: without this the handler fires on every frame of a
            # fling and queues a page request per frame.
            scroll_interval=200,
        )
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
                self._header,
                self.rows,
                self.footer,
            ],
            spacing=10,
            expand=True,
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
        )

    def _sync_header(self) -> None:
        """Mark the active column. Sorting is always descending.

        The arrow is a Material icon rather than a "↓" in the label: the web
        build's font has no glyph for it and renders a tofu box, whereas the
        icon font is bundled and works on both platforms.
        """
        for key, cell in self._sort_cells.items():
            cell.visible = key in self._layout.columns
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

    def attach(self, feed: PoolFeed) -> None:
        """Point the view at a (new) feed, e.g. after a chain change."""
        self.feed = feed
        self._sort = DEFAULT_SORT
        self.search.value = ""
        self._sync_header()
        self.rows.controls = []
        self._sync_count()

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

    def _search_changed(self, e: ft.ControlEvent) -> None:
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

    def _scrolled(self, e: ft.OnScrollEvent) -> None:
        """Pull the next page when the end comes into view."""
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
            return
        if feed.total is None:
            self.count_label.value = "Loading…"
        elif feed.exhausted:
            self.count_label.value = f"{feed.total} pools"
        else:
            self.count_label.value = f"{feed.loaded} of {feed.total} pools"

    def _run(self, handler, *args) -> None:
        """Schedule an async handler.

        `run_task` insists on a coroutine *function*: it rejects a bare
        coroutine object with a TypeError, which is why every call site
        passes the method and its arguments separately.
        """
        self._page.run_task(handler, *args)
