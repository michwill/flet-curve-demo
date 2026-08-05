"""The pool list: search, sortable columns, one row per pool.

Sorted by 24h volume by default, matching Curve's own UI -- volume is the
best single proxy for "which pools are actually being used". The ordering
rules themselves live in `curve.sort` so they can be tested without a UI.

Rows are rendered into a `ListView`, which virtualises: a chain like
Ethereum returns ~380 pools above $10k TVL and building every row as a
materialised control makes the first paint visibly slow.
"""

from __future__ import annotations

from typing import Callable

import flet as ft

from curve.format import apr_range, compact_usd, percent
from curve.models import Pool
from curve.sort import SORTS, DEFAULT_SORT, search_pools, sort_pools

from . import safe_update

#: Column widths, shared by the header and every row so they line up.
W_BASE = 110
W_REWARDS = 190
W_VOLUME = 130
W_TVL = 130


class PoolRow(ft.Container):
    """One pool. Click anywhere to open it."""

    def __init__(self, pool: Pool, on_open: Callable[[Pool], None]) -> None:
        self.pool = pool

        title = ft.Text(pool.display_name, size=14, weight=ft.FontWeight.W_500)
        coins = ft.Text(
            " ".join(pool.coin_symbols),
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        name_cell = ft.Column([title, coins], spacing=1, expand=True)

        base = ft.Text(percent(pool.base_apr), size=13, text_align=ft.TextAlign.RIGHT)

        # CRV first, then each incentive token on its own line -- the same
        # shape Curve uses, and it keeps a pool with three reward tokens from
        # squeezing the other columns.
        reward_lines: list[ft.Control] = []
        if pool.crv_apr[1] > 0:
            reward_lines.append(
                ft.Text(
                    f"{apr_range(*pool.crv_apr)} CRV",
                    size=12,
                    text_align=ft.TextAlign.RIGHT,
                )
            )
        for incentive in pool.incentives:
            reward_lines.append(
                ft.Text(
                    f"{percent(incentive.apr)} {incentive.symbol}",
                    size=12,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    text_align=ft.TextAlign.RIGHT,
                )
            )
        if not reward_lines:
            reward_lines.append(
                ft.Text("–", size=13, color=ft.Colors.OUTLINE, text_align=ft.TextAlign.RIGHT)
            )

        super().__init__(
            content=ft.Row(
                [
                    name_cell,
                    ft.Container(base, width=W_BASE, alignment=ft.Alignment.CENTER_RIGHT),
                    ft.Container(
                        ft.Column(
                            reward_lines,
                            spacing=0,
                            horizontal_alignment=ft.CrossAxisAlignment.END,
                        ),
                        width=W_REWARDS,
                        alignment=ft.Alignment.CENTER_RIGHT,
                    ),
                    ft.Container(
                        ft.Text(compact_usd(pool.volume_24h), size=13),
                        width=W_VOLUME,
                        alignment=ft.Alignment.CENTER_RIGHT,
                    ),
                    ft.Container(
                        ft.Text(compact_usd(pool.tvl), size=13),
                        width=W_TVL,
                        alignment=ft.Alignment.CENTER_RIGHT,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
            on_click=lambda _e: on_open(pool),
            ink=True,
        )


class PoolListView(ft.Column):
    """Search box, sortable header, and the rows."""

    def __init__(self, on_open: Callable[[Pool], None]) -> None:
        self._on_open = on_open
        self._pools: list[Pool] = []
        self._sort = DEFAULT_SORT
        self._query = ""

        self.search = ft.TextField(
            hint_text="Search name, symbol or paste an address",
            prefix_icon=ft.Icons.SEARCH,
            on_change=self._search_changed,
            dense=True,
            border_radius=8,
        )
        self.count_label = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.rows = ft.ListView(expand=True, spacing=0)
        self._header = self._build_header()

        super().__init__(
            controls=[
                ft.Row(
                    [ft.Container(self.search, expand=True), self.count_label],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=16,
                ),
                self._header,
                self.rows,
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
        container also gives the whole cell as a hit target instead of just
        the text, which is the better affordance anyway.
        """
        self._sort_cells: dict[str, ft.Container] = {}
        widths = {"base": W_BASE, "incentives": W_REWARDS, "volume": W_VOLUME, "tvl": W_TVL}
        cells: list[ft.Control] = [
            ft.Container(
                ft.Text("Pool", size=12, color=ft.Colors.ON_SURFACE_VARIANT), expand=True
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
            option = next(o for o in SORTS if o.key == key)
            active = key == self._sort
            label = ft.Text(
                option.label,
                size=12,
                weight=ft.FontWeight.BOLD if active else ft.FontWeight.NORMAL,
                color=ft.Colors.PRIMARY if active else ft.Colors.ON_SURFACE_VARIANT,
            )
            cell.content = (
                ft.Row(
                    [label, ft.Icon(ft.Icons.ARROW_DOWNWARD, size=13, color=ft.Colors.PRIMARY)],
                    spacing=2,
                    tight=True,
                    alignment=ft.MainAxisAlignment.END,
                )
                if active
                else ft.Row([label], tight=True, alignment=ft.MainAxisAlignment.END)
            )

    # -- data -------------------------------------------------------------

    def set_pools(self, pools: list[Pool]) -> None:
        self._pools = pools
        self._render()

    def _sort_by(self, key: str) -> None:
        self._sort = key
        self._sync_header()
        self._render()

    def _search_changed(self, e: ft.ControlEvent) -> None:
        self._query = e.control.value or ""
        self._render()

    def _render(self) -> None:
        visible = sort_pools(search_pools(self._pools, self._query), self._sort)
        self.rows.controls = [PoolRow(p, self._on_open) for p in visible]
        total = len(self._pools)
        self.count_label.value = (
            f"{len(visible)} of {total} pools" if self._query else f"{total} pools"
        )
        safe_update(self)
