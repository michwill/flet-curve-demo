"""One pool: its price history, what is in it, and what you can do to it.

Laid out like Curve's own pool page because the arrangement is genuinely
good -- chart and composition on the left, a single action panel pinned on
the right -- and because a side-by-side comparison is the point of building
an alternative UI at all.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

import flet as ft

from curve import explorers, parameters
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
from curve.merkl import MerklCampaign
from curve.models import Coin, Pool
from curve.pool import PoolContract
from wallet.base import WalletError

from . import AnyEvent, safe_update, theme
from .actions import ClaimTab, DepositTab, StakeTab, SwapTab, WithdrawTab
from .candles import CandleChart
from .logos import pool_stack, token_mark
from .pool_list import POINTS_ICON
from .responsive import Layout, layout_for
from .typography import BODY, LABEL, METRIC, SMALL, TITLE, TITLE_NARROW

LP_SERIES = "__lp__"


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


#: The parameter list is indented under its heading, not against the
#: chart's left edge, so the fold reads as one block.
PARAMETER_PADDING = ft.Padding.only(left=6, bottom=4)

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
        # `ft.Column` exposes `page` as a read-only property, so the reference
        # kept for `run_task`/`update` needs a name of its own.
        self._page = page
        self.api = api
        self.pool = pool
        self.get_contract = get_contract
        #: The chain's own explorer, where the chain publishes one. Lite
        #: chains do; the rest come from a table. See `curve.explorers`.
        self._explorer = explorer

        # Set before anything is built: the address rows ask it whether
        # they have room to print 42 characters.
        self._layout = layout_for(2000.0)

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
        #: Whether that slot holds a table yet, rather than the line above
        #: or an error. A width change re-lays the table out; it must not
        #: turn "loading" into an empty one.
        self._composition_ready = False
        self._yields_slot = ft.Container(self._yields())
        # Empty for most pools, and empty until the campaign lookups have
        # landed for the rest -- they ride alongside the pool list rather
        # than blocking it, so a deep link can paint before they arrive.
        self._campaigns_slot = ft.Container(self._campaigns())
        # Read from the pool itself, so it cannot be built until the
        # provider has answered. The rows are replaced in place -- this
        # container outlives the reads and is never re-made, which is what
        # keeps it assignable (see `theme.rows_theme` for what happens to
        # controls that are not).
        self._parameter_rows = ft.Column(spacing=2)
        self._parameters_slot = ft.Container(self._parameters())

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
                self._campaigns_slot,
                self._parameters_slot,
            ],
            spacing=10,
        )
        self._right = ft.Container(self._actions())
        # Side by side on a wide window, stacked on a narrow one. Rebuilt by
        # `set_layout` rather than being two separate trees, so the chart and
        # the action panel keep their state across a resize.
        self._body = ft.Container()

        self._on_back = on_back
        #: Rebuilt when the page crosses into card widths -- the name needs
        #: its own line there, and a Row cannot be made into a Column.
        self._header_slot = ft.Container(self._header())

        super().__init__(
            controls=[self._header_slot, self._body],
            spacing=14,
        )
        self._arrange()

    # -- layout -----------------------------------------------------------

    def set_layout(self, layout: Layout) -> None:
        was = self._layout
        self._layout = layout
        if layout.cards != was.cards:
            # Addresses are written in full where there is room and
            # shortened where there is not, so this crossing is the one
            # that re-makes them. The rows of values are the same object
            # either way and keep whatever has been read.
            self._parameters_slot.content = self._parameters()
            # And the two that lay out by width rather than by content.
            self._header_slot.content = self._header()
            if self._composition_ready:
                self._composition_slot.content = self._composition()
        if layout.stacked == was.stacked:
            safe_update(self)
            return
        self._arrange()
        safe_update(self)

    def _arrange(self) -> None:
        """Chart beside the actions, or above them when there is no room.

        Neither arrangement scrolls: the window does -- see `CurveApp.body`
        -- and this page is laid out to whatever height it needs inside it.
        That is also why nothing here may be `expand` on the vertical: a
        flex child in unbounded height is a Flutter layout error rather
        than a cosmetic problem, and it is what broke this page at phone
        widths before it stopped scrolling for itself.

        The action panel is as tall as what is in it, in both
        arrangements: nothing on this page is given a height. See
        `_actions` for why not.
        """
        self.scroll = None
        self._left.scroll = None
        self._left.expand = False
        self._body.expand = False
        if self._layout.stacked:
            # In a Column `expand` is the *vertical* flex, and there is no
            # vertical to divide: the page is as tall as it needs to be.
            self._right.expand = False
            self._body.content = ft.Column(
                [self._left, self._right], spacing=16, tight=True
            )
        else:
            # In a Row it is the horizontal one, and dropping it is what
            # collapsed this page: the panel took its natural width, the
            # chart column got what was left, which was nothing, and the
            # composition set itself one letter per line.
            self._right.expand = 1
            self._body.content = ft.Row(
                [ft.Container(self._left, expand=2), self._right],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=20,
            )

    # -- header -----------------------------------------------------------

    def _header(self) -> ft.Control:
        """Back, marks, name and the two figures -- on one line or three.

        On a phone it has to be three. Everything here has a width of its
        own except the name, so the name is what gives: back button, marks
        and two figures come to about 370px, which on a 390px screen left
        the title a column one character wide and Flutter did as it was
        told -- `DAI/USDC/USDT` set vertically, one letter per line. That
        is the whole bug, and no font size fixes it. The name gets its own
        line instead, and the figures theirs.
        """
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
                # Under the name, and aligned with it rather than with the
                # back button, which is furniture.
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
        # Right-aligned at the end of a wide row, left-aligned when they
        # have their own line under the name.
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

        rows: list[ft.Control] = [] if self._layout.cards else [header]
        # The contract's coins: `balances` lines up with these, and a
        # metapool's decomposed extras have no balance of their own.
        for coin in self.pool.pool_coins:
            if self._layout.cards:
                rows.append(self._composition_card(coin, total))
                continue
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

    def _composition_card(self, coin, total: float) -> ft.Control:
        """One asset on two lines instead of four columns.

        Price, share and balance come to 340px of fixed width, which on a
        phone leaves the asset column about ten pixels -- so the symbol set
        itself one letter per line, exactly as the title did. The pool list
        and the portfolio already answer this with cards; this is the same
        answer in the same shape.
        """
        return ft.Container(
            ft.Column(
                [
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
                                expand=True,
                            ),
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
        """What Merkl pays, with the two sides of a campaign named.

        The list column prints the better of the two rates and leaves it
        there, because it has 190 pixels. Here there is room to say which
        side is which, and it is worth saying: a campaign paying only
        unstaked liquidity is one that staking switches off, and this is
        the page with a Stake button on it.

        Points get no row at all. They have no rate, so a row here would
        be a label against a blank in a column of percentages; they are
        named in the campaigns block below instead, which is also where
        the link to go and count them lives.
        """
        rows: list[ft.Control] = []
        for token in self.pool.merkl.tokens:
            for qualifier, apr in self.pool.merkl.sides_for(token):
                # What arrives, not what the campaign is denominated in --
                # see `curve.merkl.MerklToken`. The wrapper is named in
                # the campaigns block below rather than lost.
                label = f"{token.paid_symbol} via Merkl"
                rows.append(
                    self._yield_row(
                        f"{label} ({qualifier})" if qualifier else label, percent(apr)
                    )
                )
        if not self.pool.merkl and self.pool.merkle_apr > 0:
            # Curve's own figure, for a chain Merkl does not cover or a
            # request that did not come back. It names no token, which is
            # the whole reason the rows above exist.
            rows.append(self._yield_row("Merkle campaign", percent(self.pool.merkle_apr)))
        return rows

    # -- campaigns --------------------------------------------------------

    def _campaigns(self) -> ft.Control:
        """Where to go for the rewards this app cannot hand over.

        Two kinds end up here and both need a door rather than a button.
        A Merkl campaign is claimed on Merkl -- it is a merkle drop, not a
        gauge, so `claim_rewards()` knows nothing about it -- and points
        are not claimable anywhere by anybody; what you can do with points
        is see how many you have, on whoever's dashboard is counting them.

        So every row here is a link out, and the section disappears
        entirely when there is nothing to link to, which is most pools.
        """
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
        """The second line of a Merkl row: what it pays, and how fast.

        This is the one place the wrapper is written out. Merkl's own page
        -- the one the link goes to -- will say `ybwcrvUSD` where this app
        says crvUSD, so the sentence has to hold both or the two cannot be
        reconciled by whoever follows the link.
        """
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
        """One campaign: what it is, and the way out to it.

        `ft.Url` with a blank target rather than a bare string, for the
        same reason the explorer links use one -- this app is a page
        somebody was in the middle of.
        """
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
        """The addresses and the curve's own numbers, folded away.

        Collapsed by default: this is reference material, wanted rarely
        and precisely when it is wanted. It replaces the line of facts
        that used to sit under the yields, which spent two thirds of its
        width on the registry name and the word "plain" -- neither of
        which anybody can act on.
        """
        rows: list[ft.Control] = [
            self._address_row("Pool", self.pool.address),
        ]
        if self.pool.has_gauge:
            rows.append(self._address_row("Gauge", self.pool.gauge))
        rows.append(self._parameter_rows)
        return ft.ExpansionTile(
            title=ft.Text("Pool parameters", size=LABEL, weight=ft.FontWeight.BOLD),
            controls=[ft.Container(ft.Column(rows, spacing=6), padding=PARAMETER_PADDING)],
            tile_padding=ft.Padding.symmetric(horizontal=0),
            controls_padding=ft.Padding.only(bottom=6),
            dense=True,
            min_tile_height=34,
        )

    def _address_row(self, label: str, address: str) -> ft.Control:
        """An address, in full where there is room, with a copy and a link.

        Shortened only on a narrow layout. On a phone the full 42
        characters wrap onto three lines and push the row off the screen;
        on anything wider they are what someone came here for -- an
        address you have to hover or click to read is not much use for
        checking a contract.
        """
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
                    # `Url` rather than a bare string so it opens in a new
                    # tab: this app is a page you were in the middle of.
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
        """Ask the pool what shape it is.

        Ten reads, and a pool answers between three and nine of them --
        which ones is the pool's own answer to what family it belongs to,
        and is not inferable from the registry name. See
        `curve.parameters`.

        With no wallet these go through a public node, which may not
        exist for the chain; the section then keeps the addresses and says
        so, because they are the half that needs no chain at all.
        """
        contract = self.get_contract()
        if contract is None:
            self._parameter_rows.controls = [self._unread("Connect a wallet to read them.")]
            safe_update(self._parameter_rows)
            return
        try:
            values = await contract.parameters()
        except WalletError as exc:
            self._parameter_rows.controls = [self._unread(str(exc))]
            safe_update(self._parameter_rows)
            return
        self._parameter_rows.controls = [
            self._parameter_row(parameter, shown)
            for parameter, shown in parameters.rows(values)
        ] or [self._unread("This pool answered none of them.")]
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
        """The four panels, with a tab bar built by hand.

        **Nothing here is given a height.** Flet's `Tabs`/`TabBar`/
        `TabBarView` set needs one: `TabBarView` takes its height from the
        box around it and cannot size to its content, so the panel had to
        be told how tall it was -- a number worked out from how many coins
        a pool has and how tall a row measures *here*. On a phone, with
        another font and another OS, the content came out taller than the
        number and Flutter drew the Deposit button straight over the
        slippage box.

        A guessed size cannot be made right by calibrating it; it can only
        be removed. So the bar is a Row of containers, the body is one
        container holding whichever tab is showing, and every height in the
        panel comes from what is in it.

        Containers rather than buttons for the same reason the nav links
        and the sortable column headings are: a `TextButton` here hovers
        correctly and never fires its handler in the published web build.
        And the labels wrap rather than scroll, so a wider font takes a
        second line instead of a scrollbar.
        """
        self.tabs = [
            DepositTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            WithdrawTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            SwapTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            StakeTab(self._page, self.pool, self.get_contract, self.refresh_actions),
            # Last, and conditional like Stake: a claim is the tail of a
            # position rather than a way into one, and the tab is only
            # there when something is actually owed.
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
            # Chad only, and hard-edged: see `ui/theme.py`. Everywhere
            # else this is None and Material's own flatness stands.
            shadow=theme.panel_shadow(self._page),
        )

    def _sync_tabs(self) -> None:
        """Draw the bar for the tab that is showing, and its panel.

        Only the tabs that have something to act on: `Stake` takes itself
        out of the bar when the wallet holds no LP and the gauge holds none
        either. Indices stay those of `self.tabs` rather than of what is
        drawn, so which panel is showing survives a tab appearing or
        disappearing underneath it -- which happens on every confirmed
        transaction, since that is when the balances are re-read.
        """
        shown = [(index, tab) for index, tab in enumerate(self.tabs) if tab.available]
        if not any(index == self._tab for index, _tab in shown):
            # The tab that was showing has nothing left to do -- unstaking
            # the last of a position, say. Fall back to the first that has.
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
            # Underlined rather than merely coloured, as the page links in
            # the header are: colour alone is a weak signal.
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
        """Re-read every panel, then redraw the bar in case it changed.

        This runs after each confirmed transaction -- `ActionTab.on_done`
        -- and confirmation already means the reads that follow are at or
        after the block the transaction landed in, so a first deposit's LP
        is visible here rather than one refresh later. See
        `curve.confirm.wait_for_confirmation`.

        The bar is redrawn because whether `Stake` belongs in it is a
        function of those balances: staking the last unstaked LP removes
        nothing, but withdrawing the last of a position does.
        """
        for tab in self.tabs:
            await tab.refresh()
        before = self._tab
        self._sync_tabs()
        safe_update(self._tab_bar)
        if self._tab != before:
            safe_update(self._tab_body)

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
                # `i:j` is the pair as it is written and read: coin i
                # priced in coin j.
                i, j = (int(x) for x in value.split(":"))
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

    async def _load_campaigns(self) -> None:
        """Re-run the campaign lookup now that the LP token is known.

        Cheap: both sources are cached on the API client for the session,
        so a pool reached from the list asks nothing here. It is run at
        all because the list endpoint carries no `lp_token_address`, and
        an old-registry pool's LP token is a different contract from the
        pool -- which is one of the three addresses a Merkl campaign can
        be watching. A pool reached by deep link had it already.
        """
        await self.api.attach_campaigns(
            self.pool.chain_id, self.pool.chain, [self.pool]
        )
        self._campaigns_slot.content = self._campaigns()

    async def _load_detail(self) -> None:
        """Fetch the fields only the detail endpoint has, then redraw.

        The composition table and every write action depend on this: there
        is no LP token to withdraw or stake without it, and no reserves to
        tabulate.
        """
        # A Lite pool arrives complete -- reserves, prices, LP token and
        # all -- because that API has no list/detail split to fill in.
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
        await self.load_parameters()
