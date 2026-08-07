"""One pool: its price history, what is in it, and what you can do to it.

Laid out like Curve's own pool page because the arrangement is genuinely
good -- chart and composition on the left, a single action panel pinned on
the right -- and because a side-by-side comparison is the point of building
an alternative UI at all.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from curve.api import CANDLE_SIZES, DEFAULT_CANDLE_SIZE, CurveApi, get_candle_size
from curve.format import (
    apr_range,
    compact_usd,
    percent,
    price,
    short_address,
    token_amount,
)
from curve.http import ApiError
from curve.models import Pool
from curve.pool import PoolContract

from . import AnyEvent, safe_update, theme
from .actions import DepositTab, StakeTab, SwapTab, WithdrawTab
from .candles import CandleChart
from .logos import pool_stack, token_mark
from .responsive import Layout, layout_for
from .typography import BODY, LABEL, METRIC, SMALL, TITLE

LP_SERIES = "__lp__"

#: Height the action panel gets when stacked under the chart. Fixed because
#: the page scrolls in that arrangement, and a flex child inside unbounded
#: height is a Flutter layout error.
STACKED_ACTIONS_HEIGHT = 560


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

        self.chart = CandleChart(height=340, on_capacity_change=self._chart_resized)
        self.chart_caption = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.chart_error = ft.Text("", size=LABEL, color=ft.Colors.ERROR)
        self._candle_size = DEFAULT_CANDLE_SIZE

        self.series = ft.Dropdown(
            options=self._series_options(),
            value=LP_SERIES,
            dense=True,
            width=220,
            on_select=self._series_changed,
        )
        # Filled in by `_load_detail`: the v2 list endpoint carries no
        # reserves, no per-coin prices and no LP token, so there is nothing
        # to draw here until the detail request lands.
        self._composition_slot = ft.Container(
            ft.Text("Loading pool details…", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        )
        self._yields_slot = ft.Container(self._yields())

        # A dropdown rather than a segmented button: nine candle sizes do
        # not fit as buttons, and it is the control Curve uses for the same
        # job. You pick the candle, not the window -- the window follows
        # from it (200 candles of whatever size).
        self.size_picker = ft.Dropdown(
            key="candle-size",
            options=[
                ft.DropdownOption(key=size.label, text=size.label)
                for size in CANDLE_SIZES
            ],
            value=self._candle_size,
            width=110,
            dense=True,
            on_select=self._size_changed,
        )

        # A Curve Lite chain has no OHLC endpoint at all -- no service
        # indexes its trades -- so the chart and its two pickers are not
        # merely empty there, they have nothing to ask. One line stands in
        # their place; everything below it works exactly as it does
        # anywhere else, because the rest comes from the pool itself.
        chart_block: list[ft.Control] = (
            [
                ft.Container(
                    ft.Text(
                        "No price history: Curve Lite chains have no trade "
                        "indexing behind them.",
                        size=SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    padding=ft.Padding.symmetric(vertical=10),
                )
            ]
            if pool.lite
            else [
                ft.Row(
                    [self.series, ft.Container(expand=True), self.size_picker],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.chart_caption,
                self.chart,
                self.chart_error,
            ]
        )
        self._left = ft.Column(
            [
                *chart_block,
                self._composition_slot,
                self._yields_slot,
            ],
            spacing=10,
        )
        self._right = ft.Container(self._actions())
        # Side by side on a wide window, stacked on a narrow one. Rebuilt by
        # `set_layout` rather than being two separate trees, so the chart and
        # the action panel keep their state across a resize.
        self._body = ft.Container(expand=True)
        self._layout = layout_for(2000.0)

        super().__init__(
            controls=[self._header(on_back), self._body],
            spacing=14,
            expand=True,
        )
        self._arrange()

    # -- layout -----------------------------------------------------------

    def set_layout(self, layout: Layout) -> None:
        if layout.stacked == self._layout.stacked:
            self._layout = layout
            return
        self._layout = layout
        self._arrange()
        safe_update(self)

    def _arrange(self) -> None:
        """Chart beside the actions, or above them when there is no room.

        The two arrangements need opposite scrolling, which is why this
        rebuilds rather than just reflowing. Side by side, the page is a
        fixed frame and the left column scrolls inside it. Stacked, the
        *page* scrolls -- and then nothing inside it may be `expand`, because
        a flex child in unbounded height is a Flutter layout error, not a
        cosmetic problem. That is what broke the pool page at phone widths.
        """
        if self._layout.stacked:
            self.scroll = ft.ScrollMode.AUTO
            self._left.scroll = None
            self._left.expand = False
            self._right.expand = False
            # Bounded, because the page around it is not.
            self._right.height = STACKED_ACTIONS_HEIGHT
            self._body.expand = False
            self._body.content = ft.Column(
                [self._left, self._right], spacing=16, tight=True
            )
        else:
            self.scroll = None
            self._left.scroll = ft.ScrollMode.AUTO
            self._left.expand = True
            self._right.height = None
            self._right.expand = 1
            self._body.expand = True
            self._body.content = ft.Row(
                [ft.Container(self._left, expand=2), self._right],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=20,
                expand=True,
            )

    # -- header -----------------------------------------------------------

    def _header(self, on_back: Callable[[], None]) -> ft.Control:
        return ft.Row(
            [
                ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _e: on_back()),
                pool_stack(self.pool, size=38),
                ft.Column(
                    [
                        ft.Text(self.pool.display_name, size=TITLE, weight=ft.FontWeight.BOLD),
                        ft.Text(
                            " / ".join(self.pool.coin_symbols),
                            size=SMALL,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                        ),
                    ],
                    spacing=0,
                    expand=True,
                ),
                self._stat("TVL", compact_usd(self.pool.tvl)),
                # Nothing counts trades on a Lite chain, so there is no
                # volume to report -- and "$0" would read as a quiet day.
                *(
                    []
                    if self.pool.lite
                    else [self._stat("24h volume", compact_usd(self.pool.volume_24h))]
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=18,
        )

    def _stat(self, label: str, value: str) -> ft.Control:
        return ft.Column(
            [
                ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(value, size=METRIC, weight=ft.FontWeight.W_500),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.END,
        )

    # -- left column ------------------------------------------------------

    def _composition(self) -> ft.Control:
        """What is in the pool, as plain Rows rather than a `DataTable`.

        `DataTable` sizes itself from its content and does not shrink, so on
        a narrow window it overflowed its parent -- which Flutter raises as a
        widget exception, failing the integration tests outright rather than
        just looking wrong. Everything else in this app lays out with
        `Row` + fixed-width `Container` cells, which flex down cleanly; this
        now does the same.
        """
        total = sum(c.balance_usd for c in self.pool.pool_coins) or 1.0

        def cell(control: ft.Control, width: int | None = None, end: bool = False) -> ft.Control:
            return ft.Container(
                control,
                width=width,
                expand=width is None,
                alignment=ft.Alignment.CENTER_RIGHT if end else ft.Alignment.CENTER_LEFT,
            )

        header = ft.Container(
            ft.Row(
                [
                    cell(ft.Text("Asset", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)),
                    cell(ft.Text("Price", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT), 110, True),
                    cell(ft.Text("Share", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT), 80, True),
                    cell(ft.Text("Balance", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT), 150, True),
                ]
            ),
            padding=ft.Padding.only(bottom=4),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )

        rows: list[ft.Control] = [header]
        # The contract's coins: `balances` lines up with these, and a
        # metapool's decomposed extras have no balance of their own.
        for coin in self.pool.pool_coins:
            rows.append(
                ft.Container(
                    ft.Row(
                        [
                            cell(
                                ft.Row(
                                    [
                                        token_mark(coin, self.pool.chain, 26),
                                        ft.Column(
                                            [
                                                ft.Text(coin.symbol, size=BODY),
                                                ft.Text(
                                                    short_address(coin.address),
                                                    size=LABEL,
                                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                                ),
                                            ],
                                            spacing=0,
                                        ),
                                    ],
                                    spacing=8,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                )
                            ),
                            cell(ft.Text(price(coin.usd_price), size=BODY), 110, True),
                            cell(
                                ft.Text(
                                    f"{coin.balance_usd / total * 100:.2f}%", size=BODY
                                ),
                                80,
                                True,
                            ),
                            cell(
                                ft.Column(
                                    [
                                        ft.Text(token_amount(coin.balance), size=BODY),
                                        ft.Text(
                                            compact_usd(coin.balance_usd),
                                            size=LABEL,
                                            color=ft.Colors.ON_SURFACE_VARIANT,
                                        ),
                                    ],
                                    spacing=0,
                                    horizontal_alignment=ft.CrossAxisAlignment.END,
                                ),
                                150,
                                True,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.Padding.symmetric(vertical=6),
                )
            )

        return ft.Column(
            [ft.Text("COMPOSITION", size=LABEL, weight=ft.FontWeight.BOLD), *rows],
            spacing=2,
        )

    def _yields(self) -> ft.Control:
        # Base vAPY is fees earned, which needs somebody counting trades.
        # On a Lite chain nobody is, so the row would be a nought standing
        # for an unknown -- and the total below it would inherit that.
        lines: list[ft.Control] = (
            []
            if self.pool.lite
            else [self._yield_row("Base vAPY", percent(self.pool.base_apr))]
        )
        if self.pool.crv_apr[1] > 0:
            lines.append(self._yield_row("CRV (min to max boost)", apr_range(*self.pool.crv_apr)))
        for incentive in self.pool.incentives:
            lines.append(self._yield_row(f"{incentive.symbol} incentives", percent(incentive.apr)))
        if len(lines) > 1:
            lines.append(
                self._yield_row(
                    "Total (max boost)", percent(self.pool.total_apr), bold=True
                )
            )
        elif not lines:
            lines.append(
                ft.Text(
                    "Rewards are not measured on this chain.",
                    size=SMALL,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                )
            )

        facts = ft.Text(
            f"{self.pool.registry}  ·  {'metapool' if self.pool.is_meta else 'plain'}"
            f"  ·  {'gauge ' + short_address(self.pool.gauge) if self.pool.has_gauge else 'no gauge'}"
            + (f"  ·  A = {self.pool.amplification:,.0f}" if self.pool.amplification else ""),
            size=LABEL,
            color=ft.Colors.ON_SURFACE_VARIANT,
        )
        return ft.Column(
            [ft.Text("YIELD", size=LABEL, weight=ft.FontWeight.BOLD), *lines, facts],
            spacing=4,
        )

    def _yield_row(self, label: str, value: str, *, bold: bool = False) -> ft.Control:
        weight = ft.FontWeight.BOLD if bold else ft.FontWeight.NORMAL
        return ft.Row(
            [
                ft.Text(label, size=SMALL, weight=weight, expand=True),
                ft.Text(value, size=SMALL, weight=weight),
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
        # Flet 0.86 splits this into three controls: `Tabs` is the
        # container and owns `length`, `TabBar` holds the labels, and
        # `TabBarView` holds the bodies. A `Tab` is only the button -- it
        # takes no content.
        #
        # `TabBarView` takes its height from the surrounding box rather than
        # a fixed one: given `height=520` it raised a widget exception in a
        # Flutter debug build, which passes unnoticed in a release web build
        # but fails the integration tests outright.
        return ft.Container(
            ft.Tabs(
                length=len(self.tabs),
                selected_index=0,
                expand=True,
                content=ft.Column(
                    [
                        ft.TabBar(tabs=[ft.Tab(label=ft.Text(tab.title, size=BODY)) for tab in self.tabs]),
                        ft.TabBarView(
                            expand=True,
                            controls=[
                                # A panel taller than the box scrolls rather
                                # than overflowing; `Container` has no
                                # `scroll`, so the Column carries it.
                                ft.Container(
                                    ft.Column(
                                        [tab.mount()], scroll=ft.ScrollMode.AUTO
                                    ),
                                    padding=14,
                                )
                                for tab in self.tabs
                            ],
                        ),
                    ],
                    spacing=0,
                    expand=True,
                ),
            ),
            bgcolor=ft.Colors.SURFACE,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            # Chad only, and hard-edged: see `ui/theme.py`. Everywhere
            # else this is None and Material's own flatness stands.
            shadow=theme.panel_shadow(self._page),
        )

    async def refresh_actions(self) -> None:
        for tab in self.tabs:
            await tab.refresh()

    # -- chart ------------------------------------------------------------

    def _series_options(self) -> list[ft.DropdownOption]:
        options = [ft.DropdownOption(key=LP_SERIES, text="LP token (USD)")]
        for i, main in enumerate(self.pool.pool_coins):
            for j, reference in enumerate(self.pool.pool_coins):
                if i == j:
                    continue
                options.append(
                    ft.DropdownOption(
                        key=f"{i}:{j}", text=f"{main.symbol} / {reference.symbol}"
                    )
                )
        return options

    def _series_changed(self, _e: AnyEvent) -> None:
        self._page.run_task(self.load_chart)

    def _chart_resized(self) -> None:
        """The chart got materially wider or narrower -- refetch to suit.

        A wider chart should show *more* candles at the same size, not the
        same candles stretched.
        """
        self._page.run_task(self.load_chart)

    def _size_changed(self, _e: AnyEvent) -> None:
        self._candle_size = self.size_picker.value or DEFAULT_CANDLE_SIZE
        self._page.run_task(self.load_chart)

    async def load_chart(self) -> None:
        if self.pool.lite:
            return  # nothing to ask; the panel says so instead
        size = get_candle_size(self._candle_size)
        # As many candles as the chart has room for at a readable pitch,
        # rather than a fixed number that looks cramped on one size and
        # sparse on another.
        count = self.chart.candle_capacity()
        self.chart_error.value = ""
        self.chart_caption.value = "Loading…"
        self._page.update()

        try:
            value = self.series.value or LP_SERIES
            if value == LP_SERIES:
                candles = await self.api.lp_candles(
                    self.pool.chain, self.pool.address, size=size, count=count
                )
            else:
                i, j = (int(x) for x in value.split(":"))
                candles = await self.api.pair_candles(
                    self.pool.chain,
                    self.pool.address,
                    self.pool.pool_coins[i].address,
                    self.pool.pool_coins[j].address,
                    size=size,
                    count=count,
                )
        except ApiError as exc:
            self.chart.set_candles([])
            self.chart_caption.value = ""
            self.chart_error.value = str(exc)
            self._page.update()
            return

        self.chart.set_candles(candles)
        self.chart_caption.value = (
            f"{self.chart.summary}   ·   {len(candles)} x {size.label}"
            if candles
            else "No price history for this pair."
        )
        self._page.update()

    async def _load_detail(self) -> None:
        """Fetch the fields only the detail endpoint has, then redraw.

        The composition table and every write action depend on this: there
        is no LP token to withdraw or stake without it, and no reserves to
        tabulate.
        """
        # A Lite pool arrives complete -- reserves, prices, LP token and
        # all -- because that API has no list/detail split to fill in.
        if self.pool.detailed:
            self._composition_slot.content = self._composition()
            self._yields_slot.content = self._yields()
            return
        try:
            raw = await self.api.pool_detail(self.pool.chain_id, self.pool.address)
        except ApiError as exc:
            self._composition_slot.content = ft.Text(
                f"Could not load pool details: {exc}", size=SMALL, color=ft.Colors.ERROR
            )
            self._page.update()
            return
        self.pool.merge_detail(raw)
        self._composition_slot.content = self._composition()
        self._yields_slot.content = self._yields()
        # Both of these were built from the *decomposed* coin list, because
        # the contract's coin count only arrives with the detail. On a
        # metapool that is four fields where the pool takes two -- and the
        # deposit would have been calldata for a function it does not have.
        self._right.content = self._actions()
        self.series.options = self._series_options()
        self.series.value = LP_SERIES
        self._page.update()

    async def load(self) -> None:
        # Detail first: the action panels read the LP token it supplies.
        await self._load_detail()
        await self.load_chart()
        await self.refresh_actions()
