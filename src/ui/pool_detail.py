"""One pool: its price history, what is in it, and what you can do to it."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import flet as ft

from curve import depth, explorers, parameters
from curve.api import (
    CANDLE_SIZES,
    DEFAULT_CANDLE_SIZE,
    ActivityFeed,
    CurveApi,
    LiquidityFeed,
    TradeFeed,
    get_candle_size,
)
from curve.format import (
    apr_range,
    compact_usd,
    percent,
    price,
    short_address,
    token_amount,
)
from curve.http import ApiError
from curve.liquidity import DepthError
from curve.merkl import MerklCampaign
from curve.models import Coin, Pool
from curve.pool import PoolCallFailed, PoolContract
from wallet.base import WalletError

from . import AnyEvent, activity, safe_update, theme
from .actions import ClaimTab, DepositTab, StakeTab, SwapTab, WithdrawTab
from .candles import CandleChart
from .depthchart import DepthChart
from .logos import OVERLAP, coin_stack, pool_stack, token_mark
from .pool_list import POINTS_ICON
from .responsive import Layout, layout_for
from .typography import (
    BODY,
    LABEL,
    METRIC,
    SMALL,
    TITLE,
    TITLE_NARROW,
    text_width,
)

#: What the fold says while it is reading. Not "Reading them from the
#: pool...", which is what it said and which has no antecedent: the fold's
#: title is above it and out of the reader's eye by the time the words
#: appear, so "them" is a pronoun for nothing.
READING = "Reading pool parameters…"

#: How long the whole batch gets before the panel gives up on it.
PARAMETER_DEADLINE = 45.0

LP_SERIES = "__lp__"

#: What the LP series is called. The currency is worth saying where there
#: is room for it and is the first thing to go where there is not: a phone
#: has 200px for the picker, marks and arrow included.
LP_LABEL = "LP token (USD)"
LP_LABEL_NARROW = "LP token"

#: The two picker entries that put a table where the chart is, and the
#: rule that separates them from the price series above.
TRADES_SERIES = "__trades__"
LIQUIDITY_SERIES = "__liquidity__"

#: What names each of the two in the menu, where every other row carries
#: coin marks: swapped arrows for the trades, a drop for the liquidity.
ACTIVITY_MARKS = {
    TRADES_SERIES: ft.Icons.SWAP_HORIZ,
    LIQUIDITY_SERIES: ft.Icons.WATER_DROP,
}
ACTIVITY_SERIES = (TRADES_SERIES, LIQUIDITY_SERIES)
SERIES_RULE = "__rule__"

#: What a depth entry's key starts with; the rest is the pair, as `i:j`.
#: One entry per ordered pair, because a pool with three coins has six curves
#: and which one is being drawn has to be visible without opening the menu.
DEPTH_PREFIX = "__depth__"

#: How the depth chart is labelled, and how many samples it asks for.  160 is
#: about a sample every two pixels at the widths this chart is drawn at, and
#: each one costs a bisection over the invariant.
DEPTH_LABEL = "Depth"
DEPTH_POINTS = 160

#: How long the price window has to sit still before the profile is solved
#: again for it.  A wheel arrives as a burst of notches and each one would
#: otherwise cost a few hundred invariant solves.
DEPTH_SETTLE = 0.35

#: The share of a balance used to read the pool's own marginal price.
DEPTH_PROBE = 1_000_000

#: How large the two coin marks are on the flip control.  Smaller than the
#: menu's, which sit beside text; these sit beside an arrow.
FLIP_MARK = 20

#: The units the depth axis can be read in: dollars, or any of the pool's
#: coins as `coin:<index>`.
DEPTH_USD = "usd"
DEPTH_COIN = "coin"


def _unit_coin(key: str | None) -> int | None:
    """Which coin a units key names, or `None` for anything else."""
    if not key or not key.startswith(f"{DEPTH_COIN}:"):
        return None
    try:
        return int(key.split(":", 1)[1])
    except ValueError:
        return None


def price_pair(key: str) -> tuple[int, int] | None:
    """The pair a price entry names, or `None` if it is not one.

    Bare `i:j`, which is what tells it from a depth entry and from the named
    series -- `LP`, the rules, Trades and Liquidity are none of them numbers.
    """
    i, sep, j = key.partition(":")
    if not sep:
        return None
    try:
        return int(i), int(j)
    except ValueError:
        return None


def depth_pair(key: str) -> tuple[int, int] | None:
    """The pair a depth entry names, or `None` if it is not one."""
    if not key.startswith(DEPTH_PREFIX):
        return None
    i, _, j = key[len(DEPTH_PREFIX):].partition(":")
    try:
        return int(i), int(j)
    except ValueError:
        return None

#: The chart's height, which the tables take over unchanged so the page
#: does not jump when the picker moves between them.
CHART_HEIGHT = 340

#: How near the end of a table brings the page behind it in: two thirds of
#: the box, so the rows are there by the time the scroll reaches them.
ACTIVITY_SCROLL_THRESHOLD = 220

#: How large a mark is in the series menu, beside what it names.
SERIES_MARK = 18

#: A pair's two overlapping marks are wider than one glyph, and Material
#: starts a row's label after whatever its leading control is. So the two
#: tables' glyphs are boxed to a pair's width, or their names would sit
#: left of every price row's. `logos.OVERLAP` is how much of each mark the
#: next one covers, which is what makes two of them 1.66 marks wide.
SERIES_MARK_BOX = SERIES_MARK * (2 - OVERLAP)

#: And on the closed field, where it is larger and boxed. Material drops a
#: leading icon into a slot meant for one 24px glyph: a stack left raw
#: there rides the top-left corner and touches the frame. A box wider and
#: taller than the widest stack centres it instead, and holds the label
#: still while the selection moves between two coins and three.
FIELD_MARK = 22
FIELD_BOX = (56, 28)

#: How wide the picker is: sized to the longest name this pool actually has,
#: rather than to the longest any pool might.  A fixed width has to fit
#: `Depth: scrvUSD / PYUSD` and then wears that width on a pool whose rows
#: read `Depth: ETH / USDC`, which is most of them.
#:
#: Bounded above only.  There is no floor to set: `LP token (USD)` is in
#: every pool's menu and is longer than a short pool's depth rows, so it puts
#: one there by itself.  The ceiling stops the field crowding the two controls
#: to its right; past it Material clips the label, which is the better of two
#: bad answers for a pool whose symbols run that long.
SERIES_MAX_WIDTH = 360
SERIES_NARROW_WIDTH = 200

#: What sits beside the text inside the field: the marks on the left, and the
#: arrow and Material's own padding on the right.
#:
#: The 44 is the arrow and Material's padding, measured by rendering the
#: longest label and widening until it stopped losing its last character:
#: `Depth: scrvUSD / PYUSD` clipped to `... PYUSI` at 44 and fits at 60.
SERIES_CHROME = FIELD_BOX[0] + 60

#: What size the field's own label is drawn at.  Not `SMALL`: Material draws
#: a dropdown's text at its body size whatever the rows around it use, and
#: sizing the box at 13 while it drew at 16 clipped `Depth: scrvUSD / P`.
PICKER_TEXT = 16


def _metric(label: str, value: str) -> ft.Control:
    """A caption and its figure, side by side. As `ui.portfolio` draws them."""
    return ft.Row(
        [
            ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
            ft.Text(value, size=SMALL),
        ],
        spacing=4,
        tight=True,
    )


#: The parameter list is indented under its heading, not against the chart's
#: left edge, so the fold reads as one block.
PARAMETER_PADDING = ft.Padding.only(left=6, bottom=4)


def _expanded(event: AnyEvent) -> bool:
    """Did an `ExpansionTile` just open, from its `on_change` event."""
    data = event.data
    return data if isinstance(data, bool) else str(data).strip().lower() == "true"

class PoolDetailView(ft.Column):
    """The detail page. Owns its own data loading."""

    def __init__(
        self,
        page: ft.Page,
        api: CurveApi,
        pool: Pool,
        get_contract: Callable[[], PoolContract | None],
        on_back: Callable[[], None],
        explorer: str = "",
    ) -> None:
        # `ft.Column` exposes `page` as a read-only property, so the
        # reference kept for `run_task`/`update` needs a name of its
        # own.
        self._page = page
        self.api = api
        self.pool = pool
        self.get_contract = get_contract
        self._explorer = explorer

        self._layout = layout_for(2000.0)

        self.chart = CandleChart(
            height=CHART_HEIGHT, on_capacity_change=self._chart_resized
        )
        self.activity = ft.Column(
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            on_scroll=self._activity_scrolled,
            scroll_interval=200,
        )
        #: What the table has read so far, kept so a change of width can
        #: draw it again without asking for it again.
        self._activity_rows: list = []
        self._activity_kind = ""
        #: One cursor per table, kept so that going to the chart and back
        #: -- or between the two tables -- does not lose the history that
        #: was scrolled up.
        self._activity_feeds: dict[str, ActivityFeed] = {}
        #: Last in the table, and what says the older rows are coming.
        self.activity_footer = ft.Container(
            ft.Row(
                [ft.ProgressRing(width=16, height=16), ft.Text("Loading…", size=SMALL)],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
            ),
            padding=12,
            visible=False,
        )
        self.activity_box = ft.Container(
            self.activity, height=CHART_HEIGHT, visible=False
        )
        self.chart_caption = ft.Text(
            "", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT, visible=False
        )
        self.chart_error = ft.Text("", size=LABEL, color=ft.Colors.ERROR)
        self._candle_size = DEFAULT_CANDLE_SIZE

        self.series = ft.Dropdown(
            options=self._series_options(),
            value=LP_SERIES,
            leading_icon=self._field_mark(LP_SERIES),
            dense=True,
            # Room for the marks as well as the longest name: the box on
            # the left is 56 of it.
            width=SERIES_NARROW_WIDTH,  # replaced below, once the rows exist
            on_select=self._series_changed,
        )
        self.series.width = self._picker_width()
        self._composition_slot = ft.Container(
            ft.Text("Loading pool details…", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)
        )
        self._composition_ready = False
        self._yields_slot = ft.Container(self._yields())
        self._campaigns_slot = ft.Container(self._campaigns())
        self._parameter_rows = ft.Column(spacing=2)
        self._parameters_open = False
        self._parameters_asked = False
        self._parameters_slot = ft.Container(self._parameters())

        self.depth_chart = DepthChart(
            height=CHART_HEIGHT, on_window_change=self._depth_window_changed)
        self.depth_chart.visible = False
        #: What the depth axis is read in.  Dollars by default: it is the one
        #: unit that means the same thing across every pool on the page.
        #: Turns the pair round.  The menu names one ordering per pair and
        #: this is the other, because they are one curve read from either end.
        #: The two marks sit either side of it in the order being drawn, so
        #: the control shows the direction as well as changing it.
        self._flip_left = ft.Container(width=FLIP_MARK, height=FLIP_MARK)
        self._flip_right = ft.Container(width=FLIP_MARK, height=FLIP_MARK)
        self.flip_button = ft.Container(
            ft.Row(
                [self._flip_left,
                 ft.Icon(ft.Icons.SWAP_HORIZ, size=16,
                         color=ft.Colors.ON_SURFACE_VARIANT),
                 self._flip_right],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            border_radius=8,
            ink=True,
            visible=False,
            on_click=self._flip_clicked,
        )
        #: Whether the pair on screen is being read the other way round.
        #: One flag for both charts: it is the same question of either.
        self._flipped = False
        self.depth_units = ft.Dropdown(
            key="depth-units",
            options=[ft.DropdownOption(key=DEPTH_USD, text="USD")],
            value=DEPTH_USD,
            width=110,
            dense=True,
            visible=False,
            on_select=self._depth_units_changed,
        )
        self._depth_window: tuple[float, float] = (0.0, 0.0)
        self._depth_asked = 0.0
        #: The pool's own numbers and the pair's quoted price, once read.
        #: Kept because the first is the pool's rather than the pair's.
        self._depth_reading: tuple[depth.Reading, float] | None = None
        self._depth_fee = 0

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

        self._controls_slot = ft.Container(self._chart_controls())

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
                self._controls_slot,
                self.chart_caption,
                self.chart,
                self.depth_chart,
                self.activity_box,
                self.chart_error,
            ]
        )
        self._left = ft.Column(
            [
                *chart_block,
                self._composition_slot,
                self._yields_slot,
                self._campaigns_slot,
                self._parameters_slot,
            ],
            spacing=10,
        )
        self._right = ft.Container(self._actions())
        self._body = ft.Container()

        self._on_back = on_back
        self._header_slot = ft.Container(self._header())

        super().__init__(
            controls=[self._header_slot, self._body],
            spacing=14,
        )
        self._arrange()

    # -- layout -----------------------------------------------------------

    def _chart_controls(self) -> ft.Control:
        """The picker, and whatever belongs beside it.

        On a phone they go under it instead.  The picker alone is most of a
        360px screen, and the depth chart brings two companions rather than
        the candle chart's one -- so the row ran off the edge and took the
        units with it.
        """
        beside: list[ft.Control] = [
            self.flip_button, self.depth_units, self.size_picker]
        if self._layout.cards:
            # Nothing sits beside it now, so it takes the width rather than a
            # number: a phone is 320 to 430 across and `expand` fits them all,
            # where any constant fits one of them.
            self.series.width = None
            self.series.expand = True
            return ft.Column(
                [ft.Row([self.series]),
                 ft.Row(beside, spacing=6, wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER)],
                spacing=6,
            )
        self.series.expand = False
        self.series.width = self._picker_width()
        return ft.Row(
            [self.series, ft.Container(expand=True), *beside],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def set_layout(self, layout: Layout) -> None:
        was = self._layout
        self._layout = layout
        if layout.cards != was.cards:
            self.series.options = self._series_options()
            self._controls_slot.content = self._chart_controls()
            self._parameters_slot.content = self._parameters()
            self._header_slot.content = self._header()
            if self._composition_ready:
                self._composition_slot.content = self._composition()
            if self._activity_kind:
                self._draw_activity()
        if layout.stacked == was.stacked:
            safe_update(self)
            return
        self._arrange()
        safe_update(self)

    def _arrange(self) -> None:
        """Chart beside the actions, or above them when there is no room."""
        self.scroll = None
        self._left.scroll = None
        self._left.expand = False
        self._body.expand = False
        if self._layout.stacked:
            self._right.expand = False
            self._body.content = ft.Column(
                [self._left, self._right], spacing=16, tight=True
            )
        else:
            self._right.expand = 1
            self._body.content = ft.Row(
                [ft.Container(self._left, expand=2), self._right],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=20,
            )

    # -- header -----------------------------------------------------------

    def _header(self) -> ft.Control:
        """Back, marks, name and the two figures -- on one line or three."""
        title = ft.Column(
            [
                ft.Text(
                    self.pool.display_name,
                    size=TITLE if not self._layout.cards else TITLE_NARROW,
                    weight=ft.FontWeight.BOLD,
                ),
                ft.Text(
                    " / ".join(self.pool.coin_symbols),
                    size=SMALL,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
            spacing=0,
            expand=True,
        )
        back = ft.IconButton(ft.Icons.ARROW_BACK, on_click=lambda _e: self._on_back())
        stats = [self._stat(label, value) for label, value in self._stat_pairs()]
        if not self._layout.cards:
            return ft.Row(
                [back, pool_stack(self.pool, size=38), title, *stats],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=18,
            )
        return ft.Column(
            [
                ft.Row(
                    [back, pool_stack(self.pool, size=32), title],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10,
                ),
                ft.Row(stats, spacing=24, wrap=True),
            ],
            spacing=8,
        )

    def _stat_pairs(self) -> list[tuple[str, str]]:
        pairs = [("TVL", compact_usd(self.pool.tvl))]
        if not self.pool.lite:
            pairs.append(("24h volume", compact_usd(self.pool.volume_24h)))
        return pairs

    def _stat(self, label: str, value: str) -> ft.Control:
        # Right-aligned at the end of a wide row, left-aligned when
        # they have their own line under the name.
        end = not self._layout.cards
        return ft.Column(
            [
                ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(value, size=METRIC, weight=ft.FontWeight.W_500),
            ],
            spacing=0,
            horizontal_alignment=(
                ft.CrossAxisAlignment.END if end else ft.CrossAxisAlignment.START
            ),
        )

    # -- left column ------------------------------------------------------

    def _composition(self) -> ft.Control:
        """What is in the pool, as plain Rows rather than a `DataTable`."""
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

        rows: list[ft.Control] = [] if self._layout.cards else [header]
        for coin in self.pool.pool_coins:
            if self._layout.cards:
                rows.append(self._composition_card(coin, total))
                continue
            rows.append(
                ft.Container(
                    ft.Row(
                        [
                            cell(self._asset_cell(coin)),
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

    def _asset_cell(self, coin) -> ft.Control:
        """A coin's mark, symbol and address, linked to the block explorer.

        The address is already printed under the symbol, which is exactly the
        thing somebody wants to go and look up -- so the whole cell is the
        link rather than a separate icon beside it, and the row keeps its
        four columns.
        """
        url = explorers.address_url(self.pool.chain_id, coin.address,
                                    self._explorer)
        inside = ft.Row(
            [
                token_mark(coin, self.pool.chain, 26),
                ft.Column(
                    [
                        ft.Text(coin.symbol, size=BODY),
                        ft.Text(short_address(coin.address), size=LABEL,
                                color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                    spacing=0,
                    expand=True,
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        if not url:
            return inside
        return ft.Container(
            inside,
            url=ft.Url(url, target=ft.UrlTarget.BLANK),
            tooltip=f"{coin.symbol} on the explorer",
            border_radius=6,
            ink=True,
        )

    def _composition_card(self, coin, total: float) -> ft.Control:
        """One asset on two lines instead of four columns."""
        return ft.Container(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(self._asset_cell(coin), expand=True),
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
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        [
                            _metric("Price", price(coin.usd_price)),
                            _metric(
                                "Share", f"{coin.balance_usd / total * 100:.2f}%"
                            ),
                        ],
                        spacing=16,
                        wrap=True,
                    ),
                ],
                spacing=4,
            ),
            padding=ft.Padding.symmetric(vertical=8),
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT)),
        )

    def _yields(self) -> ft.Control:
        # Base vAPY is fees earned, which needs somebody counting
        # trades.
        lines: list[ft.Control] = (
            []
            if self.pool.lite
            else [self._yield_row("Base vAPY", percent(self.pool.base_apr))]
        )
        if self.pool.crv_apr[1] > 0:
            lines.append(self._yield_row("CRV (min to max boost)", apr_range(*self.pool.crv_apr)))
        for incentive in self.pool.incentives:
            lines.append(self._yield_row(f"{incentive.symbol} incentives", percent(incentive.apr)))
        lines += self._merkl_rows()
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

        return ft.Column(
            [ft.Text("YIELD", size=LABEL, weight=ft.FontWeight.BOLD), *lines],
            spacing=4,
        )

    def _merkl_rows(self) -> list[ft.Control]:
        """What Merkl pays, with the two sides of a campaign named."""
        rows: list[ft.Control] = []
        for token in self.pool.merkl.tokens:
            for qualifier, apr in self.pool.merkl.sides_for(token):
                label = f"{token.paid_symbol} via Merkl"
                rows.append(
                    self._yield_row(
                        f"{label} ({qualifier})" if qualifier else label, percent(apr)
                    )
                )
        if not self.pool.merkl and self.pool.merkle_apr > 0:
            rows.append(self._yield_row("Merkle campaign", percent(self.pool.merkle_apr)))
        return rows

    # -- campaigns --------------------------------------------------------

    def _campaigns(self) -> ft.Control:
        """Where to go for the rewards this app cannot hand over."""
        rows: list[ft.Control] = []
        for campaign in self.pool.merkl.all:
            token = next(iter(campaign.tokens), None)
            rows.append(
                self._campaign_row(
                    mark=(
                        ft.Icon(POINTS_ICON, size=20, color=ft.Colors.ON_SURFACE_VARIANT)
                        if token is None or token.points
                        else token_mark(
                            Coin(
                                address=token.paid_address,
                                symbol=token.paid_symbol,
                                decimals=18,
                            ),
                            self.pool.chain,
                            20,
                        )
                    ),
                    title=campaign.name or "Merkl campaign",
                    detail=self._campaign_detail(campaign),
                    url=campaign.url,
                    link="Open on Merkl",
                )
            )
        for external in self.pool.points:
            rows.append(
                self._campaign_row(
                    mark=ft.Icon(POINTS_ICON, size=20, color=ft.Colors.ON_SURFACE_VARIANT),
                    title=external.label,
                    detail=external.describe(),
                    url=external.dashboard,
                    link=f"Open {external.platform}",
                )
            )
        if not rows:
            return ft.Container()
        return ft.Column(
            [
                ft.Text("CAMPAIGNS", size=LABEL, weight=ft.FontWeight.BOLD),
                ft.Text(
                    "Paid outside the gauge. Merkl rewards are claimed on "
                    "Merkl; points are counted by whoever is giving them.",
                    size=LABEL,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                *rows,
            ],
            spacing=4,
        )

    def _campaign_detail(self, campaign: MerklCampaign) -> str:
        """The second line of a Merkl row: what it pays, and how fast."""
        tokens = ", ".join(token.paid_symbol for token in campaign.tokens) or "rewards"
        wrapped = [t for t in campaign.tokens if t.wrapped]
        via = (
            " (paid as "
            + ", ".join(t.symbol for t in wrapped)
            + " on Merkl, unwrapped when you claim)"
            if wrapped
            else ""
        )
        if campaign.points_only:
            return f"{tokens}, which carry no price and so no rate{via}"
        return f"{tokens} at {percent(campaign.apr)}{via}"

    def _campaign_row(
        self, *, mark: ft.Control, title: str, detail: str, url: str, link: str
    ) -> ft.Control:
        """One campaign: what it is, and the way out to it."""
        return ft.Row(
            [
                mark,
                ft.Column(
                    [
                        ft.Text(title, size=SMALL),
                        ft.Text(
                            detail, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT
                        ),
                    ],
                    spacing=0,
                    expand=True,
                ),
                ft.IconButton(
                    ft.Icons.OPEN_IN_NEW,
                    icon_size=14,
                    tooltip=link,
                    url=ft.Url(url, target=ft.UrlTarget.BLANK) if url else None,
                    disabled=not url,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        )

    # -- parameters -------------------------------------------------------

    def _parameters(self) -> ft.Control:
        """The addresses and the curve's own numbers, folded away."""
        rows: list[ft.Control] = [
            self._address_row("Pool", self.pool.address),
        ]
        if self.pool.has_any_gauge:
            label = "Gauge" if self.pool.has_gauge else "Gauge (retired)"
            rows.append(self._address_row(label, self.pool.any_gauge))
        rows.append(self._parameter_rows)
        return ft.ExpansionTile(
            title=ft.Text("Pool parameters", size=LABEL, weight=ft.FontWeight.BOLD),
            controls=[ft.Container(ft.Column(rows, spacing=6), padding=PARAMETER_PADDING)],
            tile_padding=ft.Padding.symmetric(horizontal=0),
            controls_padding=ft.Padding.only(bottom=6),
            dense=True,
            min_tile_height=34,
            expanded=self._parameters_open,
            on_change=self._parameters_toggled,
        )

    def _parameters_toggled(self, event: ft.Event[ft.ExpansionTile]) -> None:
        """Read the pool the first time somebody opens the fold."""
        self._parameters_open = _expanded(event)
        if self._parameters_open and not self._parameters_asked:
            self._parameters_asked = True
            self._parameter_rows.controls = [self._unread(READING)]
            safe_update(self._parameter_rows)
            self._page.run_task(self.load_parameters)

    def _address_row(self, label: str, address: str) -> ft.Control:
        """An address, in full where there is room, with a copy and a link."""
        shown = short_address(address) if self._layout.cards else address
        url = explorers.address_url(self.pool.chain_id, address, self._explorer)
        return ft.Row(
            [
                ft.Text(label, size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT, width=54),
                ft.Text(
                    shown,
                    size=SMALL,
                    font_family="monospace",
                    selectable=True,
                    expand=True,
                ),
                ft.IconButton(
                    ft.Icons.COPY_ALL_OUTLINED,
                    icon_size=14,
                    tooltip="Copy address",
                    on_click=lambda _e, value=address: self._copy(value),
                ),
                ft.IconButton(
                    ft.Icons.OPEN_IN_NEW,
                    icon_size=14,
                    tooltip="Open in the explorer",
                    url=ft.Url(url, target=ft.UrlTarget.BLANK) if url else None,
                ),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=4,
        )

    def _copy(self, value: str) -> None:
        self._page.run_task(self._copy_now, value)

    async def _copy_now(self, value: str) -> None:
        with contextlib.suppress(Exception):
            await ft.Clipboard().set(value)

    async def load_parameters(self) -> None:
        """Ask the pool what shape it is."""
        contract = self.get_contract()
        if contract is None:
            self._unreadable("Connect a wallet to read them.")
            return
        try:
            readings = await asyncio.wait_for(contract.parameters(), PARAMETER_DEADLINE)
        except TimeoutError:
            self._unreadable("The chain did not answer in time. Try again.")
            return
        except WalletError as exc:
            self._unreadable(str(exc))
            return
        except Exception as exc:
            self._unreadable(f"They could not be read: {exc}")
            return
        coins = [(coin.symbol, coin.decimals) for coin in self.pool.pool_coins]
        shown = parameters.rows(readings.values) + parameters.rate_rows(readings.rates, coins)
        self._parameter_rows.controls = [
            self._parameter_row(parameter, value) for parameter, value in shown
        ] or [self._unread("This pool answered none of them.")]
        safe_update(self._parameter_rows)

    def _unreadable(self, why: str) -> None:
        """Say why, and let the next open try again."""
        self._parameters_asked = False
        self._parameter_rows.controls = [self._unread(why)]
        safe_update(self._parameter_rows)

    def _parameter_row(self, parameter: parameters.Parameter, shown: str) -> ft.Control:
        return ft.Row(
            [
                ft.Text(
                    parameter.label,
                    size=SMALL,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    tooltip=parameter.note,
                    expand=True,
                ),
                ft.Text(shown, size=SMALL, selectable=True),
            ]
        )

    def _unread(self, why: str) -> ft.Control:
        return ft.Text(why, size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)

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
        """The four panels, with a tab bar built by hand."""
        self.tabs = [
            DepositTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            WithdrawTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            SwapTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            StakeTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            ClaimTab(self._page, self.pool, self.get_contract, self.refresh_actions),
        ]
        self._tab = 0
        self._tab_bar = ft.Container(
            border=ft.Border(bottom=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT))
        )
        self._tab_body = ft.Container(padding=14)
        self._sync_tabs()
        return ft.Container(
            ft.Column([self._tab_bar, self._tab_body], spacing=0),
            bgcolor=ft.Colors.SURFACE,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=10,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            shadow=theme.panel_shadow(self._page),
        )

    def _sync_tabs(self) -> None:
        """Draw the bar for the tab that is showing, and its panel."""
        shown = [(index, tab) for index, tab in enumerate(self.tabs) if tab.available]
        if not any(index == self._tab for index, _tab in shown):
            self._tab = shown[0][0] if shown else 0
        self._tab_bar.content = ft.Row(
            [self._tab_label(index, tab.title) for index, tab in shown],
            spacing=0,
            wrap=True,
        )
        self._tab_body.content = self.tabs[self._tab].mount()

    def _tab_label(self, index: int, title: str) -> ft.Control:
        here = index == self._tab
        return ft.Container(
            ft.Text(
                title,
                size=BODY,
                color=ft.Colors.PRIMARY if here else ft.Colors.ON_SURFACE_VARIANT,
                weight=ft.FontWeight.W_500 if here else None,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            border=ft.Border(bottom=ft.BorderSide(2, ft.Colors.PRIMARY)) if here else None,
            on_click=lambda _e, chosen=index: self._show_tab(chosen),
            ink=True,
        )

    def _show_tab(self, index: int) -> None:
        if index == self._tab:
            return
        self._tab = index
        self._sync_tabs()
        safe_update(self._tab_bar)
        safe_update(self._tab_body)

    async def refresh_actions(self) -> None:
        """Re-read every panel, then redraw the bar in case it changed."""
        for tab in self.tabs:
            await tab.refresh()
        before = self._tab
        self._sync_tabs()
        safe_update(self._tab_bar)
        if self._tab != before:
            safe_update(self._tab_body)

    # -- chart ------------------------------------------------------------

    def _series_mark(self, key: str, size: float = SERIES_MARK) -> ft.Control | None:
        """What names a series: the pool's own stack for the LP token, the
        two coins for a pair, and a glyph for each of the two tables.

        Built fresh each time -- a control belongs to one place in the
        tree, and this is drawn in the menu and again on the closed field.
        """
        # A depth entry is named by its pair, like the price series it sits
        # under -- and stripping the prefix here is what puts the mark on the
        # closed field too, which reads the whole key rather than the pair.
        pair = depth_pair(key)
        if pair is not None:
            key = f"{pair[0]}:{pair[1]}"
        if key == LP_SERIES:
            return pool_stack(self.pool, size=size, limit=3)
        if glyph := ACTIVITY_MARKS.get(key):
            return ft.Icon(glyph, size=size, color=ft.Colors.ON_SURFACE_VARIANT)
        coins = self.pool.pool_coins
        i, _, j = key.partition(":")
        if not j.isdigit() or not i.isdigit():
            return None
        if int(i) >= len(coins) or int(j) >= len(coins):
            return None
        return coin_stack([coins[int(i)], coins[int(j)]], self.pool.chain, size)

    def _menu_mark(self, key: str) -> ft.Control | None:
        """The same mark in the menu, where a glyph is boxed to a pair's
        width so its name starts where the price rows' names do.
        """
        mark = self._series_mark(key)
        if key not in ACTIVITY_MARKS or mark is None:
            return mark
        return ft.Container(
            mark, width=SERIES_MARK_BOX, alignment=ft.Alignment.CENTER_LEFT
        )

    def _field_mark(self, key: str) -> ft.Control | None:
        """The same marks on the closed field, boxed so they sit in it
        rather than on its corner. See `FIELD_BOX`.
        """
        mark = self._series_mark(key, FIELD_MARK)
        if mark is None:
            return None
        width, height = FIELD_BOX
        return ft.Container(
            mark, width=width, height=height, alignment=ft.Alignment.CENTER
        )

    def _picker_width(self) -> float:
        """Wide enough for this pool's longest row, and no wider.

        Estimated rather than measured: Flet cannot ask how wide a string will
        draw before it draws it, so `text_width` guesses and guesses generously
        -- a little empty space reads as a box, a little too little reads as a
        bug.
        """
        longest = max((text_width(option.text or "", PICKER_TEXT)
                       for option in self.series.options), default=0.0)
        return min(longest + SERIES_CHROME, SERIES_MAX_WIDTH)

    def _series_options(self) -> list[ft.DropdownOption]:
        options = [
            ft.DropdownOption(
                key=LP_SERIES,
                text=LP_LABEL_NARROW if self._layout.cards else LP_LABEL,
                leading_icon=self._series_mark(LP_SERIES),
            )
        ]
        # One per *unordered* pair, the way the depth entries are: a price and
        # its reciprocal are the same series read from the other end, so the
        # second ordering belongs on the button that turns it round rather
        # than on a row of its own.  Three rows for a tricrypto pool where six
        # were being offered, and six where twelve were.
        for i, main in enumerate(self.pool.pool_coins):
            for j in range(i):
                key = f"{i}:{j}"
                options.append(
                    ft.DropdownOption(
                        key=key,
                        text=f"{main.symbol} / {self.pool.pool_coins[j].symbol}",
                        leading_icon=self._series_mark(key),
                    )
                )
        # Under a rule, because these two are not a third way of drawing the
        # price: they replace the chart with what actually went through.
        options.append(
            ft.DropdownOption(key=SERIES_RULE, content=ft.Divider(height=1), disabled=True)
        )
        for key, text in ((TRADES_SERIES, "Trades"), (LIQUIDITY_SERIES, "Liquidity")):
            options.append(
                ft.DropdownOption(
                    key=key, text=text, leading_icon=self._menu_mark(key)
                )
            )
        # One per *unordered* pair.  The two orderings of a pair are one
        # curve read from either end, so the second belongs on a button
        # rather than on a row of its own -- three rows for a tricrypto pool
        # where six were being offered.  The later coin leads by default,
        # which puts the volatile one over the stable one on the pools where
        # that distinction exists.
        options.append(
            ft.DropdownOption(key=f"{SERIES_RULE}2", content=ft.Divider(height=1),
                              disabled=True)
        )
        coins = self.pool.pool_coins
        for i, main in enumerate(coins):
            for j in range(i):
                key = f"{DEPTH_PREFIX}{i}:{j}"
                options.append(
                    ft.DropdownOption(
                        key=key,
                        text=f"{DEPTH_LABEL}: {main.symbol} / {coins[j].symbol}",
                        leading_icon=self._series_mark(key),
                    )
                )
        return options

    @property
    def selection(self) -> str:
        """What the picker names right now."""
        return self.series.value or LP_SERIES

    def _series_changed(self, _e: AnyEvent) -> None:
        self.series.leading_icon = self._field_mark(self.selection)
        safe_update(self.series)
        self._page.run_task(self.load_selection)

    async def load_selection(self) -> None:
        """Draw whatever the picker names: a series, a table, or a curve."""
        if self.selection in ACTIVITY_SERIES:
            await self.load_activity()
        elif depth_pair(self.selection) is not None:
            await self.load_depth()
        else:
            await self.load_chart()

    async def load_depth(self) -> None:
        """Draw where this pool's liquidity sits along its own curve.

        Read here and nowhere else.  Nothing on this page asks the chain for
        any of it until somebody picks a curve, and then it is one batch --
        `A`, `gamma`, the rates or `price_scale`, the balances -- plus one
        `get_dy`.  No history, and nothing for the other pools on the list.

        The batch is the *pool's*, so it is kept: the six curves of a
        tricrypto pool are six readings of one set of balances, and moving
        between them re-asks only the marginal price.  That one is the pair's,
        and it is what says which curve the pool is actually on -- the API's
        type does not separate a YieldBasis pool from the cryptoswap in the
        same factory.
        """
        menu_key = self.selection
        pair = self._shown_pair()
        if pair is None:
            return
        i, j = pair
        self._show("depth")
        self._sync_flip()
        self.chart_error.value = ""
        self._sync_depth_units()
        self.depth_chart.say("Reading the pool…")
        self._page.update()
        contract = self.get_contract()
        if contract is None:
            self.depth_chart.say("No endpoint for this network.")
            return
        held = self._depth_reading
        try:
            if held is None:
                reading, fee = await asyncio.wait_for(
                    self._read_pool(contract), PARAMETER_DEADLINE)
            else:
                reading, fee = held[0], self._depth_fee
            quoted = await asyncio.wait_for(
                self._quoted_price(contract, reading.balances, fee, i, j),
                PARAMETER_DEADLINE)
        except TimeoutError:
            self.depth_chart.say("The chain did not answer in time.")
            return
        except (WalletError, PoolCallFailed) as exc:
            self.depth_chart.say(str(exc))
            return
        except Exception as exc:
            self.depth_chart.say(f"The pool could not be read: {exc}")
            return
        if self.selection != menu_key:
            return  # the picker moved while we were reading
        self._depth_reading = (reading, quoted)
        self._depth_fee = fee
        self._depth_window = (0.0, 0.0)
        await self._draw_depth(i, j)



    async def _read_pool(self, contract) -> tuple[depth.Reading, int]:
        """One batch of reads, as `curve.depth` wants them, and the fee.

        `curve_state`, not `parameters`: the panel's read fills a table and is
        asked for whenever somebody opens it.  Nothing pays for the depth
        curve except the depth curve.
        """
        coins = self.pool.pool_coins
        readings, reserves, fee = await contract.curve_state(len(coins))
        return depth.Reading(
            balances=tuple(reserves),
            decimals=tuple(coin.decimals for coin in coins),
            values=dict(readings.values),
            rates=tuple(readings.rates),
        ), fee

    async def _quoted_price(self, contract, reserves, fee: int,
                            i: int, j: int) -> float:
        """What the pool says a marginal trade costs, fee taken back out.

        One `get_dy` at a millionth of the balance: small enough to read as
        marginal, large enough not to round to nothing on a six-decimal coin.
        It is what settles which curve the pool is on, so a pool that will not
        answer gets whichever candidate came first and a caveat with it.
        """
        coins = self.pool.pool_coins
        if i >= len(reserves) or not reserves[i]:
            return 0.0
        dx = max(1, reserves[i] // DEPTH_PROBE)
        try:
            dy = await contract.get_dy(i, j, dx)
        except (WalletError, PoolCallFailed, ValueError):
            return 0.0
        if not dy:
            return 0.0
        rate = (dy / 10 ** coins[j].decimals) / (dx / 10 ** coins[i].decimals)
        return rate / max(1e-9, 1 - fee / 1e10)

    async def _draw_depth(self, i: int, j: int) -> None:
        """Solve the profile for the window on screen and hand it over."""
        held = self._depth_reading
        if held is None:
            return
        reading, quoted = held
        low, high = self._depth_window
        try:
            found, fitted = await asyncio.to_thread(
                depth.profile, reading, i, j, quoted=quoted or None,
                low=low, high=high, points=DEPTH_POINTS)
        except DepthError as exc:
            self.depth_chart.say(str(exc))
            return
        except Exception as exc:
            self.depth_chart.say(f"This curve could not be traced: {exc}")
            return
        if self._shown_pair() != (i, j):
            return
        self.depth_chart.show(self._valued(found, i), self._depth_unit_label(i),
                              keep_view=bool(low and high))
        # The pair leads, and says which way round it is: the menu row keeps
        # naming the ordering it offered, so after a flip that label is the
        # one thing on screen still saying the old direction.
        coins = self.pool.pool_coins
        self._say_chart(
            f"{coins[i].symbol} / {coins[j].symbol} · {fitted.family}"
            f" · liquidity per 1% of price range")

    def _valued(self, found, i: int):
        """The profile in whatever unit the picker names.

        The depth arrives counted in the coin being sold, which is the one the
        curve is a function of.  Dollars are that times its price; another
        coin is that times the ratio of the two prices.  Reading the same pool
        in each of its coins is worth having -- what a 1% move costs is a
        different number in BTC than in crvUSD, and both are the answer to a
        question somebody asks.
        """
        price = self.pool.pool_coins[i].usd_price
        if self.depth_units.value == DEPTH_USD:
            scale = price
        else:
            wanted = _unit_coin(self.depth_units.value)
            if wanted is None or wanted == i:
                return found
            other = self.pool.pool_coins[wanted].usd_price
            scale = price / other if price and other else 0.0
        # Nothing to do for a scale of one, and nothing that *can* be done
        # for a missing price -- though `_sync_depth_units` does not offer a
        # coin it cannot reach, so that second case should not arise.
        if not scale or scale == 1.0:
            return found
        return replace(found, samples=tuple(
            replace(sample, depth=sample.depth * scale)
            for sample in found.samples))

    def _depth_unit_label(self, i: int) -> str:
        if self.depth_units.value == DEPTH_USD:
            return "USD"
        wanted = _unit_coin(self.depth_units.value)
        at = i if wanted is None else wanted
        return self.pool.pool_coins[at].symbol

    def _sync_depth_units(self) -> None:
        """USD, then every coin in the pool that can be converted to.

        Every coin rather than just the one being sold: the reading is the
        same depth in different units, and which unit somebody wants is not
        decided by which way round the pair happens to be.  A coin with no
        price is left out, because there is no ratio to reach it by -- except
        the coin being sold, which needs no conversion at all.
        """
        pair = self._shown_pair()
        if pair is None:
            return
        coins = self.pool.pool_coins
        base = coins[pair[0]].usd_price
        options = [ft.DropdownOption(key=DEPTH_USD, text="USD")] if base else []
        for index, coin in enumerate(coins):
            if index == pair[0] or (base and coin.usd_price):
                options.append(
                    ft.DropdownOption(key=f"{DEPTH_COIN}:{index}",
                                      text=coin.symbol))
        self.depth_units.options = options
        offered = {option.key for option in options}
        if self.depth_units.value not in offered:
            self.depth_units.value = (
                DEPTH_USD if base else f"{DEPTH_COIN}:{pair[0]}")
        safe_update(self.depth_units)

    def _depth_units_changed(self, _e: AnyEvent) -> None:
        pair = self._shown_pair()
        if pair is not None:
            self._page.run_task(self._draw_depth, *pair)

    def _depth_window_changed(self, low: float, high: float) -> None:
        """The view moved; solve the curve again for what is on screen.

        Coalesced rather than debounced with a timer: a wheel burst sets the
        window a dozen times and only the last one is worth solving, so each
        wake-up checks whether it is still the newest before doing the work.
        """
        self._depth_window = (low, high)
        self._depth_asked = time.monotonic()
        self._page.run_task(self._redraw_depth_soon, self._depth_asked)

    async def _redraw_depth_soon(self, asked: float) -> None:
        await asyncio.sleep(DEPTH_SETTLE)
        if asked != self._depth_asked:
            return  # a later move has taken over
        pair = self._shown_pair()
        if pair is not None:
            await self._draw_depth(*pair)

    def _selected_pair(self) -> tuple[int, int] | None:
        """The pair the menu names, whichever chart it names it for."""
        return depth_pair(self.selection) or price_pair(self.selection)

    def _shown_pair(self) -> tuple[int, int] | None:
        """The pair being drawn, after the flip.

        The menu names one ordering and the button turns it round; the curve
        is the same one either way, read from the other end -- which is as
        true of a price and its reciprocal as it is of a depth curve.
        """
        pair = self._selected_pair()
        if pair is None:
            return None
        i, j = pair
        return (j, i) if self._flipped else (i, j)

    def _flip_clicked(self, _e: AnyEvent) -> None:
        self._flipped = not self._flipped
        self._sync_flip()
        if depth_pair(self.selection) is not None:
            self._page.run_task(self.load_depth)
        elif price_pair(self.selection) is not None:
            self._page.run_task(self.load_chart)

    def _sync_flip(self) -> None:
        """Say which way round the curve is, on the button that turns it."""
        pair = self._shown_pair()
        if pair is None:
            return
        coins = self.pool.pool_coins
        i, j = pair
        self._flip_left.content = token_mark(coins[i], self.pool.chain, FLIP_MARK)
        self._flip_right.content = token_mark(coins[j], self.pool.chain, FLIP_MARK)
        self.flip_button.tooltip = (
            f"Showing {coins[i].symbol} / {coins[j].symbol} -- "
            f"click for {coins[j].symbol} / {coins[i].symbol}"
        )
        safe_update(self._flip_left)
        safe_update(self._flip_right)
        safe_update(self.flip_button)

    def _show(self, which: str) -> None:
        """Give the chart's space to whichever of the three is showing.

        Each control brings its own second picker or none: candles have a size
        and the depth curve has its units, and a table has neither.
        """
        self.chart.visible = which == "chart"
        self.activity_box.visible = which == "activity"
        self.depth_chart.visible = which == "depth"
        self.size_picker.visible = which == "chart"
        self.depth_units.visible = which == "depth"
        # Shown for either chart, since both draw a pair the menu names one
        # ordering of.  The tables have no pair and no button.
        self.flip_button.visible = (
            which in ("depth", "chart") and self._selected_pair() is not None)

    def _show_activity(self, showing: bool) -> None:
        self._show("activity" if showing else "chart")

    def _chart_resized(self) -> None:
        """The chart got materially wider or narrower -- refetch to suit."""
        if self.selection in ACTIVITY_SERIES:
            return  # it is not on screen; its width means nothing
        self._page.run_task(self.load_chart)

    def _size_changed(self, _e: AnyEvent) -> None:
        self._candle_size = self.size_picker.value or DEFAULT_CANDLE_SIZE
        self._page.run_task(self.load_chart)

    async def load_chart(self) -> None:
        if self.pool.lite:
            return  # nothing to ask; the panel says so instead
        size = get_candle_size(self._candle_size)
        count = self.chart.candle_capacity()
        self._show_activity(False)
        self.chart_error.value = ""
        self._say_chart("Loading…")
        self._page.update()

        try:
            value = self.series.value or LP_SERIES
            if value == LP_SERIES:
                candles = await self.api.lp_candles(
                    self.pool.chain, self.pool.address, size=size, count=count
                )
            else:
                shown = self._shown_pair()
                if shown is None:
                    raise ApiError(f"{value} does not name a pair")
                i, j = shown
                candles = await self.api.pair_candles(
                    self.pool.chain,
                    self.pool.address,
                    base=self.pool.pool_coins[i].address,
                    quote=self.pool.pool_coins[j].address,
                    size=size,
                    count=count,
                )
        except ApiError as exc:
            self.chart.set_candles([])
            self._say_chart("")
            self.chart_error.value = str(exc)
            self._page.update()
            return

        self.chart.set_candles(candles)
        self._say_chart("")
        self._page.update()

    def _activity_feed(self, kind: str) -> ActivityFeed:
        """The cursor for one of the two tables, opened the first time it
        is asked for and kept afterwards.
        """
        feed = self._activity_feeds.get(kind)
        if feed is None:
            feed = (
                TradeFeed(
                    self.api,
                    self.pool.chain,
                    self.pool.address,
                    [coin.address for coin in self.pool.pool_coins],
                )
                if kind == TRADES_SERIES
                else LiquidityFeed(self.api, self.pool.chain, self.pool.address)
            )
            self._activity_feeds[kind] = feed
        return feed

    async def load_activity(self) -> None:
        """Fill the table where the chart was: swaps, or liquidity moved.

        The picker is read once, at the top: every await here is a place
        somebody can pick something else, and a table that lands after
        that would draw over whatever they chose instead.
        """
        wanted = self.selection
        self._show_activity(True)
        self.chart_error.value = ""
        feed = self._activity_feed(wanted)

        if feed.rows:  # read once already; put it back rather than ask again
            self._activity_kind = wanted
            self._activity_rows = list(feed.rows)
            self._draw_activity()
            self._say_chart("")
            self._page.update()
            return

        self.activity.controls = []
        self._say_chart("Loading…")
        self._page.update()
        if feed.loading:
            return  # the read already in flight will draw it

        try:
            await feed.load_more()
        except ApiError as exc:
            if self.selection == wanted:
                self._say_chart("")
                self.chart_error.value = str(exc)
                self._page.update()
            return

        if self.selection != wanted:
            return
        self._activity_kind = wanted
        self._activity_rows = list(feed.rows)
        self._draw_activity()
        self._say_chart("")
        self._page.update()

    def _activity_scrolled(self, e: ft.OnScrollEvent) -> None:
        """Pull the older rows in as the end of the table comes into view."""
        feed = self._activity_feeds.get(self._activity_kind)
        if feed is None or feed.loading or feed.exhausted:
            return
        if e.max_scroll_extent - e.pixels > ACTIVITY_SCROLL_THRESHOLD:
            return
        self._page.run_task(self._load_more_activity)

    async def _load_more_activity(self) -> None:
        """Read the page behind the table and add it underneath.

        Only what came back is drawn: rebuilding the whole table would
        cost the scroll position the reader is holding.
        """
        kind = self._activity_kind
        feed = self._activity_feeds.get(kind)
        if feed is None or feed.loading or feed.exhausted:
            return
        had = len(self._activity_rows)
        self.activity_footer.visible = True
        safe_update(self.activity_footer)

        try:
            fresh = await feed.load_more()
        except ApiError as exc:
            if self._activity_kind == kind:
                self.activity_footer.visible = False
                self.chart_error.value = str(exc)
                self._page.update()
            return

        if self._activity_kind != kind:
            return  # the picker moved while we waited
        self._activity_rows = list(feed.rows)
        self.activity_footer.visible = False
        if not had:
            self._draw_activity()  # the table was showing its empty line
        else:
            # The footer keeps its place at the end of the table.
            self.activity.controls[-1:] = [
                *(self._activity_row(row) for row in fresh),
                self.activity_footer,
            ]
        safe_update(self.activity)

    def _activity_row(self, row: Any) -> ft.Control:
        """One line of whichever table is up, at the width there is."""
        narrow = self._layout.cards
        if self._activity_kind == TRADES_SERIES:
            return activity.trade_row(
                row, self.pool.chain, self.pool.chain_id, self._explorer, narrow=narrow
            )
        return activity.liquidity_row(row, self.pool, self._explorer, narrow=narrow)

    def _draw_activity(self) -> None:
        """Build the table out of what was read, for the width there is.

        Kept apart from the fetch so that turning a phone sideways redraws
        the rows -- one column narrower, and the coin names back -- without
        asking the API again.
        """
        rows = [self._activity_row(row) for row in self._activity_rows]
        nothing = (
            activity.NO_TRADES
            if self._activity_kind == TRADES_SERIES
            else activity.NO_LIQUIDITY
        )
        self.activity.controls = [
            *(rows or [activity.empty(nothing)]),
            self.activity_footer,
        ]

    def _say_chart(self, message: str) -> None:
        """The line above the chart, which carries the wait and nothing else.

        It used to read the last price, its change over the window and how
        many candles were drawn. None of that is worth a line above a chart
        that shows the first two and is the third, and the chart draws its
        own "no price history" in the middle of the empty plot.
        """
        self.chart_caption.value = message
        self.chart_caption.visible = bool(message)

    async def _load_campaigns(self) -> None:
        """Re-run the campaign lookup now that the LP token is known."""
        await self.api.attach_campaigns(
            self.pool.chain_id, self.pool.chain, [self.pool]
        )
        self._campaigns_slot.content = self._campaigns()

    async def _load_detail(self) -> None:
        """Fetch the fields only the detail endpoint has, then redraw."""
        if self.pool.detailed:
            await self._load_campaigns()
            self._composition_slot.content = self._composition()
            self._composition_ready = True
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
        await self._load_campaigns()
        self._composition_slot.content = self._composition()
        self._composition_ready = True
        self._yields_slot.content = self._yields()
        self._right.content = self._actions()
        self.series.options = self._series_options()
        if self.selection not in ACTIVITY_SERIES:
            self.series.value = LP_SERIES
        self.series.leading_icon = self._field_mark(self.selection)
        self._page.update()

    async def load(self) -> None:
        # Detail first: the action panels read the LP token it supplies.
        await self._load_detail()
        await self.load_selection()
        await self.refresh_actions()
        # Not the parameters: those wait for somebody to open the
        # fold.
