"""One pool: its price history, what is in it, and what you can do to it.

Laid out like Curve's own pool page because the arrangement is genuinely
good -- chart and composition on the left, a single action panel pinned on
the right -- and because a side-by-side comparison is the point of building
an alternative UI at all.
"""

from __future__ import annotations

from typing import Awaitable, Callable

import flet as ft

from curve.api import CurveApi
from curve.format import apr_range, compact_usd, percent, short_address, token_amount
from curve.http import ApiError
from curve.models import Pool
from curve.pool import PoolContract

from .actions import DepositTab, StakeTab, SwapTab, WithdrawTab
from .candles import CandleChart

#: (label, days, agg_number, agg_units) for the timeframe picker.
RANGES: tuple[tuple[str, int, int, str], ...] = (
    ("7D", 7, 1, "hour"),
    ("30D", 30, 1, "day"),
    ("90D", 90, 1, "day"),
    ("1Y", 365, 1, "day"),
)

LP_SERIES = "__lp__"


class PoolDetailView(ft.Column):
    """The detail page. Owns its own data loading."""

    def __init__(
        self,
        page: ft.Page,
        api: CurveApi,
        pool: Pool,
        get_contract: Callable[[], PoolContract | None],
        on_back: Callable[[], None],
    ) -> None:
        # `ft.Column` exposes `page` as a read-only property, so the reference
        # kept for `run_task`/`update` needs a name of its own.
        self._page = page
        self.api = api
        self.pool = pool
        self.get_contract = get_contract

        self.chart = CandleChart(height=340)
        self.chart_caption = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.chart_error = ft.Text("", size=11, color=ft.Colors.ERROR)
        self._range = "90D"

        self.series = ft.Dropdown(
            options=self._series_options(),
            value=LP_SERIES,
            dense=True,
            width=220,
            on_select=self._series_changed,
        )
        self.range_buttons = ft.SegmentedButton(
            segments=[ft.Segment(value=label, label=ft.Text(label)) for label, *_ in RANGES],
            selected=[self._range],
            on_change=self._range_changed,
            allow_multiple_selection=False,
        )

        super().__init__(
            controls=[
                self._header(on_back),
                ft.Row(
                    [
                        ft.Container(
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            self.series,
                                            ft.Container(expand=True),
                                            self.range_buttons,
                                        ],
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    self.chart_caption,
                                    self.chart,
                                    self.chart_error,
                                    self._composition(),
                                    self._yields(),
                                ],
                                spacing=10,
                                scroll=ft.ScrollMode.AUTO,
                            ),
                            expand=True,
                        ),
                        ft.Container(self._actions(), width=360),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    spacing=20,
                    expand=True,
                ),
            ],
            spacing=14,
            expand=True,
        )

    # -- header -----------------------------------------------------------

    def _header(self, on_back: Callable[[], None]) -> ft.Control:
        return ft.Row(
            [
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _e: on_back()),
                ft.Column(
                    [
                        ft.Text(self.pool.display_name, size=22, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            " / ".join(self.pool.coin_symbols),
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    spacing=0,
                    expand=True,
                ),
                self._stat("TVL", compact_usd(self.pool.tvl)),
                self._stat("24h volume", compact_usd(self.pool.volume_24h)),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=18,
        )

    def _stat(self, label: str, value: str) -> ft.Control:
        return ft.Column(
            [
                ft.Text(label, size=10, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(value, size=18, weight=ft.FontWeight.W_500),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.END,
        )

    # -- left column ------------------------------------------------------

    def _composition(self) -> ft.Control:
        rows = []
        total = sum(c.balance_usd for c in self.pool.coins) or 1.0
        for coin in self.pool.coins:
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Column(
                                [
                                    ft.Text(coin.symbol, size=13),
                                    ft.Text(
                                        short_address(coin.address),
                                        size=10,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                ],
                                spacing=0,
                            )
                        ),
                        ft.DataCell(ft.Text(f"${coin.usd_price:,.5f}".rstrip("0"), size=13)),
                        ft.DataCell(
                            ft.Text(f"{coin.balance_usd / total * 100:.2f}%", size=13)
                        ),
                        ft.DataCell(
                            ft.Column(
                                [
                                    ft.Text(token_amount(coin.balance), size=13),
                                    ft.Text(
                                        compact_usd(coin.balance_usd),
                                        size=10,
                                        color=ft.Colors.ON_SURFACE_VARIANT,
                                    ),
                                ],
                                spacing=0,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                            )
                        ),
                    ]
                )
            )
        return ft.Column(
            [
                ft.Text("COMPOSITION", size=11, weight=ft.FontWeight.BOLD),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("Asset", size=11)),
                        ft.DataColumn(ft.Text("Price", size=11)),
                        ft.DataColumn(ft.Text("Share", size=11)),
                        ft.DataColumn(ft.Text("Balance", size=11), numeric=True),
                    ],
                    rows=rows,
                    column_spacing=24,
                    heading_row_height=32,
                    data_row_max_height=52,
                ),
            ],
            spacing=6,
        )

    def _yields(self) -> ft.Control:
        lines: list[ft.Control] = [
            self._yield_row("Base vAPY", percent(self.pool.base_apr)),
        ]
        if self.pool.crv_apr[1] > 0:
            lines.append(self._yield_row("CRV (min to max boost)", apr_range(*self.pool.crv_apr)))
        for incentive in self.pool.incentives:
            lines.append(self._yield_row(f"{incentive.symbol} incentives", percent(incentive.apr)))
        lines.append(
            self._yield_row(
                "Total (max boost)", percent(self.pool.total_apr), bold=True
            )
        )

        facts = ft.Text(
            f"{self.pool.registry}  ·  {'metapool' if self.pool.is_meta else 'plain'}"
            f"  ·  {'gauge ' + short_address(self.pool.gauge) if self.pool.has_gauge else 'no gauge'}"
            + (f"  ·  A = {self.pool.amplification}" if self.pool.amplification else ""),
            size=11,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        return ft.Column(
            [ft.Text("YIELD", size=11, weight=ft.FontWeight.BOLD), *lines, facts],
            spacing=4,
        )

    def _yield_row(self, label: str, value: str, *, bold: bool = False) -> ft.Control:
        weight = ft.FontWeight.BOLD if bold else ft.FontWeight.NORMAL
        return ft.Row(
            [
                ft.Text(label, size=12, weight=weight, expand=True),
                ft.Text(value, size=12, weight=weight),
            ]
        )

    # -- right column -----------------------------------------------------

    def _actions(self) -> ft.Control:
        self.tabs = [
            DepositTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            WithdrawTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            SwapTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            StakeTab(self._page, self.pool, self.get_contract, self.refresh_actions),
        ]
        # Flet 0.86 splits this into three controls: `Tabs` is the container
        # and owns `length`, `TabBar` holds the labels, `TabBarView` holds
        # the bodies. A `Tab` is only the button -- it takes no content.
        return ft.Container(
            ft.Tabs(
                length=len(self.tabs),
                selected_index=0,
                content=ft.Column(
                    [
                        ft.TabBar(tabs=[ft.Tab(label=tab.title) for tab in self.tabs]),
                        ft.TabBarView(
                            controls=[
                                ft.Container(tab.mount(), padding=14) for tab in self.tabs
                            ],
                            height=520,
                        ),
                    ],
                    spacing=0,
                ),
            ),
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=10,
            # TabBarView keeps every panel alive, so without clipping the
            # inactive tabs' fields bleed out past the rounded border.
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    async def refresh_actions(self) -> None:
        for tab in self.tabs:
            await tab.refresh()

    # -- chart ------------------------------------------------------------

    def _series_options(self) -> list[ft.DropdownOption]:
        options = [ft.DropdownOption(key=LP_SERIES, text="LP token (USD)")]
        for i, main in enumerate(self.pool.coins):
            for j, reference in enumerate(self.pool.coins):
                if i == j:
                    continue
                options.append(
                    ft.DropdownOption(
                        key=f"{i}:{j}", text=f"{main.symbol} / {reference.symbol}"
                    )
                )
        return options

    def _series_changed(self, _e: ft.ControlEvent) -> None:
        self._page.run_task(self.load_chart)

    def _range_changed(self, e: ft.ControlEvent) -> None:
        selected = e.control.selected
        if selected:
            self._range = next(iter(selected))
        self._page.run_task(self.load_chart)

    async def load_chart(self) -> None:
        label, days, agg_number, agg_units = next(
            r for r in RANGES if r[0] == self._range
        )
        self.chart_error.value = ""
        self.chart_caption.value = "Loading…"
        self._page.update()

        try:
            value = self.series.value or LP_SERIES
            if value == LP_SERIES:
                candles = await self.api.lp_candles(
                    self.pool.chain,
                    self.pool.address,
                    days=days,
                    agg_number=agg_number,
                    agg_units=agg_units,
                )
            else:
                i, j = (int(x) for x in value.split(":"))
                candles = await self.api.pair_candles(
                    self.pool.chain,
                    self.pool.address,
                    self.pool.coins[i].address,
                    self.pool.coins[j].address,
                    days=days,
                    agg_number=agg_number,
                    agg_units=agg_units,
                )
        except ApiError as exc:
            self.chart.set_candles([])
            self.chart_caption.value = ""
            self.chart_error.value = str(exc)
            self._page.update()
            return

        self.chart.set_candles(candles)
        self.chart_caption.value = self.chart.summary or f"{len(candles)} candles"
        self._page.update()

    async def load(self) -> None:
        await self.load_chart()
        await self.refresh_actions()
