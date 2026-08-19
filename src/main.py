"""An alternative Curve UI, written in Python with Flet."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

import flet as ft

from curve import (
    ApiError,
    CurveApi,
    Pool,
    PoolContract,
    earnings,
    http,
    portfolio,
    rewards,
)
from curve.api import PoolFeed
from curve.confirm import wait_for_confirmation
from curve.format import compact_usd
from curve.lite import LiteChain
from curve.rpc import (
    ChainlistDirectory,
    FallbackProvider,
    PublicNode,
    prefers_public_reads,
)
from curve.sort import DEFAULT_SORT
from ui import AnyEvent, buttons, logos, routing, safe_update, status
from ui import theme as themes
from ui.assets import chad_mark, chain_name, curve_logo
from ui.logos import chain_mark
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView
from ui.portfolio import PortfolioView
from ui.responsive import content_width, layout_for
from ui.typography import BODY, LABEL, ROW_TITLE, SMALL, TITLE
from wallet import (
    Wallet,
    WalletChoice,
    WalletError,
    autoconnect,
    is_browser,
)
from wallet.base import RpcError

DEFAULT_CHAIN = "ethereum"
#: Shown first in the picker; anything else the API reports is appended.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")

#: Where the progress bar sits when the pool list has arrived and the
#: balances have not been read yet.
PORTFOLIO_DISCOVERY_SHARE = 0.5

# -- opening somewhere other than the front page ---------------------------
# Two knobs for driving the app without driving the *UI*: which page to open
# on, and which theme to open in.
ROUTE_ENV = "CURVE_ROUTE"
THEME_ENV = "CURVE_THEME"
#: And how big to open the window, as WIDTHxHEIGHT.
WINDOW_ENV = "CURVE_WINDOW"


def startup_route() -> str:
    """The route to open on, or empty for whatever the platform says."""
    route = os.environ.get(ROUTE_ENV, "").strip()
    return route if route.startswith("/") else ""


def startup_theme() -> str:
    """The theme to open in, or empty to use the remembered one."""
    wanted = os.environ.get(THEME_ENV, "").strip().lower()
    return wanted if wanted in themes.NAMES else ""


def startup_window() -> tuple[float, float] | None:
    """The window size to open at, or None for the default."""
    raw = os.environ.get(WINDOW_ENV, "").strip().lower()
    width, _, height = raw.partition("x")
    try:
        size = (float(width), float(height))
    except ValueError:
        return None
    return size if size[0] > 0 and size[1] > 0 else None


#: Where the last portfolio scan is remembered, so the page has something to
#: show while the next one runs.
PORTFOLIO_KEY = "flet-curve.portfolio"

#: Where the chosen theme is remembered, per browser or per desktop install.
THEME_KEY = "flet-curve.theme"

#: What a wallet answers when it has never heard of the network being asked
#: for (EIP-3085).
UNKNOWN_CHAIN = 4902

#: The Curve mark, sized against the wordmark beside it rather than against
#: the header's height -- the two read as one lockup.
BRAND_LOGO = 34

#: How tall the bar is, stated once and given to the hover target.
HEADER_HEIGHT = 48 + 2 * 10

#: Below this the header has no room to give, so hovering does nothing -- a
#: chip that grew here would push the chain picker off the row.
ADDRESS_EXPAND_MIN_PAGE = 1100

#: The two pages the app has, as they appear in the URL and the nav.
PAGE_POOLS = "pools"
PAGE_PORTFOLIO = "portfolio"

#: Room around a page, inside the scroller rather than around it: the
#: scrollbar is drawn at the edge of the thing that scrolls, so padding the
#: scroller itself would move the bar in off the window's edge.
BODY_PADDING = 20

#: Space between the wordmark and the chain totals.
TOTALS_GAP = 22

#: Space between the mark and the first link.
NAV_GAP = 26

#: Space between the links themselves. They are bold and pool-name sized
#: now, so they need more air between them than a caption would.
NAV_SPACING = 14

#: How wide the nav slides open: the gap, plus both links.
NAV_WIDTH = NAV_GAP + NAV_SPACING + 210

#: Below this the header has no room to slide anything open, so the mark
#: becomes a menu button instead.
NAV_EXPAND_MIN_PAGE = 900

#: What the browser tab and the desktop window are called.
APP_TITLE = "Curve Finance"

#: The connect button's resting label. It is swapped rather than blanked
#: while connecting: a `Button` with an `icon` and no `content` refuses to
#: render at all.
CONNECT_LABEL = "Connect wallet"

# -- a header that fits a phone --------------------------------------------
CHAIN_PICKER_WIDTH = 185
CHAIN_PICKER_NARROW_WIDTH = 78

#: How wide the *open* menu is, whatever the closed field has shrunk to.
CHAIN_MENU_WIDTH = 200

#: How big a theme's face is drawn on the button, and in the menu it moves
#: into on a phone.
BUTTON_MARK = 22
MENU_MARK = 20

#: The network mark inside the picker. Smaller than a token mark elsewhere:
#: a dense dropdown's field is barely taller than its text.
CHAIN_ICON = 14


def chain_icon(chain: str) -> ft.Control | None:
    """The selected network's mark, inset from the field's border."""
    mark = chain_mark(chain, CHAIN_ICON, sized_by_parent=True)
    if mark is None:
        return None
    return ft.Container(mark, padding=ft.Padding.only(left=10, right=4))


def wallet_mark(icon: str | None, name: str, size: float = 28) -> ft.Control:
    """The wallet software's own face, or a generic wallet glyph."""
    fallback = ft.CircleAvatar(
        content=ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET, size=size * 0.6),
        radius=size / 2,
    )
    if not icon:
        return fallback
    return ft.Image(
        src=icon,
        width=size,
        height=size,
        fit=ft.BoxFit.CONTAIN,
        error_content=fallback,
    )


class CurveApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.api = CurveApi()
        self.wallet: Wallet | None = None
        self._route_applied = False
        self._chainlist = ChainlistDirectory()
        self._public_nodes: dict[int, PublicNode] = {}
        self.chain = DEFAULT_CHAIN
        self.chains: dict[str, int] = {}
        self._lite_chains: dict[str, LiteChain] = {}
        self.feed: PoolFeed | None = None
        self._detail: PoolDetailView | None = None
        self._page_name = PAGE_POOLS
        self._opened_from = PAGE_POOLS
        self._address_expanded = False

        self.storage = ft.SharedPreferences()

        self._build()
        forced = startup_theme()
        if forced:
            self._set_theme(forced, remember=False)
        else:
            page.run_task(self.restore_theme)
        opening = startup_route()
        if opening:
            page.route = opening
        page.on_route_change = self._route_changed
        if not is_browser():
            page.run_task(self.dress_window)
        page.run_task(self.load_pools)
        if autoconnect():
            self.connect_button.visible = False
            page.run_task(self.connect, None)
        elif is_browser():
            page.run_task(self.restore)

    async def dress_window(self) -> None:
        """Put the Curve mark on the desktop window, where that is possible."""
        from ui.window_icon import apply_window_icon

        for _attempt in range(6):
            try:
                if apply_window_icon():
                    return
            except Exception:  # pragma: no cover - never worth a crash
                return
            await asyncio.sleep(0.5)

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        page = self.page
        page.title = APP_TITLE
        page.padding = 0
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = themes.material()
        page.dark_theme = themes.material()
        page.window.width, page.window.height = startup_window() or (1280.0, 900.0)

        self._icons = False
        self._chain_order: list[str] = list(PREFERRED_CHAINS)
        self._totals: list[tuple[str, str]] = []

        self.chain_picker = ft.Dropdown(
            options=[self._chain_option(c) for c in PREFERRED_CHAINS],
            value=self.chain,
            width=CHAIN_PICKER_WIDTH,
            menu_width=CHAIN_MENU_WIDTH,
            dense=True,
            border=ft.InputBorder.OUTLINE,
            leading_icon=chain_icon(self.chain),
            on_select=self._chain_changed,
        )
        self.totals = ft.Text(
            "",
            size=SMALL,
            color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=True,
            animate_opacity=ft.Animation(
                duration=ft.Duration(milliseconds=160),
                curve=ft.AnimationCurve.EASE_OUT,
            ),
        )
        logo = curve_logo()
        self.brand = (
            ft.Image(
                key="brand",
                src=logo,
                width=BRAND_LOGO,
                height=BRAND_LOGO,
                fit=ft.BoxFit.CONTAIN,
                filter_quality=ft.FilterQuality.MEDIUM,
                error_content=ft.Text("CURVE", size=TITLE, weight=ft.FontWeight.BOLD),
            )
            if logo
            else ft.Text("CURVE", key="brand", size=TITLE, weight=ft.FontWeight.BOLD)
        )
        self.nav = ft.Container(
            width=0,
            padding=ft.Padding.only(left=NAV_GAP),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            animate=ft.Animation(
                duration=ft.Duration(milliseconds=160),
                curve=ft.AnimationCurve.EASE_OUT,
            ),
        )
        self.menu = ft.PopupMenuButton(
            icon=ft.Icons.MENU,
            visible=False,
            tooltip="Pages",
        )
        self._sync_nav()

        self.build_label = ft.Text(
            "Curve",
            size=TITLE,
            weight=ft.FontWeight.BOLD,
            tooltip=f"{'browser' if is_browser() else 'desktop'} build",
        )
        self.account_label = ft.Text("", size=SMALL, no_wrap=True)
        self.account_chip = ft.Container(
            self.account_label,
            visible=False,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border=themes.panel_border(page),
            border_radius=8,
            alignment=ft.Alignment.CENTER_LEFT,
            ink=True,
            on_click=self._wallet_clicked,
            on_hover=self._account_hovered,
            animate_size=ft.Animation(
                duration=ft.Duration(milliseconds=140),
                curve=ft.AnimationCurve.EASE_OUT,
            ),
        )
        self.connect_button = buttons.Themed(
            CONNECT_LABEL,
            page=page,
            icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
            on_click=self.connect,
        )
        self.connect_icon = buttons.StandIn(
            self.connect_button,
            lambda: True,
            icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
            tooltip=CONNECT_LABEL,
            on_click=self.connect,
        )
        self.theme_button = ft.Container(
            on_click=self._toggle_theme,
            ink=True,
            border_radius=22,
            padding=8,
            alignment=ft.Alignment.CENTER,
        )
        self._sync_theme_button()
        page.on_platform_brightness_change = lambda _e: self._sync_theme_button(update=True)
        page.on_resize = self._resized

        brand = ft.Container(
            ft.Row([self.brand, self.build_label], spacing=8, tight=True),
            on_click=lambda _e: self.go_page(PAGE_POOLS),
            ink=True,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=4, vertical=2),
        )
        lockup = ft.Container(
            ft.Row(
                [
                    brand,
                    self.nav,
                    ft.Container(
                        self.totals, expand=True, padding=ft.Padding.only(left=TOTALS_GAP)
                    ),
                ],
                spacing=0,
            ),
            on_hover=self._brand_hovered,
            expand=True,
            height=HEADER_HEIGHT,
        )
        self._header_box = ft.Container(
            ft.Row(
                [
                    self.menu,
                    lockup,
                    self.account_chip,
                    self.connect_icon,
                    self.chain_picker,
                    self.theme_button,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=14,
            ),
            expand=True,
        )
        self.header = ft.Container(
            ft.Row([self._header_box], alignment=ft.MainAxisAlignment.CENTER, spacing=0),
            padding=ft.Padding.symmetric(horizontal=20),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            shadow=themes.bar_shadow(page),
        )

        self.list_view = PoolListView(page, on_open=self.open_pool)
        self.portfolio_view = PortfolioView(
            page, on_open=self.open_holding, on_claim=self.claim_portfolio
        )
        self._earnings: list[earnings.Earning] = []
        self._earning_seeds: tuple | None = None
        self.progress = ft.ProgressBar(visible=False)
        self.error = ft.Text("", size=SMALL, color=ft.Colors.ERROR, visible=False)
        self._showing: ft.Control = self.list_view
        self._page_box = ft.Container(self.list_view, padding=BODY_PADDING, expand=True)
        self.body = ft.ListView(
            controls=[
                ft.Row([self._page_box], alignment=ft.MainAxisAlignment.CENTER, spacing=0)
            ],
            expand=True,
            on_scroll=self._body_scrolled,
            scroll_interval=200,
        )

        page.add(
            ft.Column(
                [self.header, self.progress, self.error, self.body],
                spacing=0,
                expand=True,
            )
        )

    def _show(self, view: ft.Control) -> None:
        """Put a page in the body, at the top of it."""
        self._page_box.content = view
        self._showing = view
        self.page.run_task(self._to_top)

    async def _to_top(self) -> None:
        """Back to the top of the page. Harmless before it is mounted."""
        with contextlib.suppress(Exception):
            await self.body.scroll_to(offset=0)

    def _body_scrolled(self, e: ft.OnScrollEvent) -> None:
        """The page scrolled. Only the pool list wants to know."""
        if self._showing is self.list_view:
            self.list_view.page_scrolled(e)

    def _menu_items(self) -> list[ft.PopupMenuItem]:
        """What the mark opens."""
        pages = [
            ft.PopupMenuItem(content=ft.Text("Pools"),
                             checked=self._page_name == PAGE_POOLS,
                             on_click=lambda _e: self.go_page(PAGE_POOLS)),
            ft.PopupMenuItem(content=ft.Text("Portfolio"),
                             checked=self._page_name == PAGE_PORTFOLIO,
                             on_click=lambda _e: self.go_page(PAGE_PORTFOLIO)),
        ]
        if not self._icons:
            return pages
        current = self._theme_name()
        items: list[ft.PopupMenuItem] = [*pages, ft.PopupMenuItem()]
        items += [
            ft.PopupMenuItem(
                icon=self._theme_mark(name, MENU_MARK),
                content=ft.Text(f"{name.capitalize()} theme"),
                checked=name == current,
                on_click=lambda _e, chosen=name: self._set_theme(chosen),
            )
            for name in themes.NAMES
        ]
        if self._totals:
            items.append(ft.PopupMenuItem())
            items += [
                ft.PopupMenuItem(content=ft.Text(f"{label} {value}"), disabled=True)
                for label, value in self._totals
            ]
        return items

    def _sync_chain_picker(self) -> None:
        """Draw the picker for the width there is: named, or a mark alone."""
        labelled = not self._icons
        self.chain_picker.options = [
            self._chain_option(chain, labelled) for chain in self._chain_order
        ]
        self.chain_picker.value = self.chain
        self.chain_picker.width = (
            CHAIN_PICKER_WIDTH if labelled else CHAIN_PICKER_NARROW_WIDTH
        )
        self.chain_picker.border = (
            ft.InputBorder.OUTLINE if labelled else ft.InputBorder.NONE
        )
        self.chain_picker.leading_icon = chain_icon(self.chain)

    def _resized(self, e: ft.PageResizeEvent) -> None:
        self._apply_layout(e.width)

    def _apply_width(self, width: float) -> None:
        """Stop the page growing once it is wider than it wants to be."""
        capped = content_width(width)
        for box in (self._header_box, self._page_box):
            box.width = capped
            box.expand = capped is None

    def _apply_layout(self, width: float | None = None) -> None:
        """Push the current layout at every view."""
        width = width or self.page.width or 0
        if not width:
            return
        media = getattr(self.page, "media", None)
        logos.set_pixel_ratio(getattr(media, "device_pixel_ratio", None))
        self._apply_width(width)
        layout = layout_for(width)
        self.totals.visible = not layout.cards
        self.build_label.visible = not layout.cards
        icons = layout.cards
        if icons != self._icons:
            self._icons = icons
            self._sync_chain_picker()
        self.theme_button.visible = not icons
        self.menu.items = self._menu_items()
        narrow = width < NAV_EXPAND_MIN_PAGE
        self.menu.visible = narrow
        if narrow:
            self.nav.width = 0
            self.totals.opacity = 1.0
        safe_update(self.header)
        safe_update(self.body)
        self.list_view.set_layout(layout)
        self.portfolio_view.set_layout(layout)
        if self._detail is not None:
            self._detail.set_layout(layout)

    def _chain_option(self, chain: str, labelled: bool = True) -> ft.DropdownOption:
        """A network's mark beside its proper name, not its API slug."""
        mark = chain_mark(chain)
        label = ft.Text(chain_name(chain), size=BODY)
        return ft.DropdownOption(
            key=chain,
            content=ft.Row([mark, label], spacing=8, tight=True) if mark else label,
            text=chain_name(chain) if labelled else "",
        )

    def _sync_theme_button(self, update: bool = False) -> None:
        """Show the theme you are *in*: sun for light, moon for dark, the
        Chad for Chad.
        """
        current = self._theme_name()
        mark = self._theme_mark(current)
        following = themes.NAMES[(themes.NAMES.index(current) + 1) % len(themes.NAMES)]
        self._set_theme_button(mark, f"{current.capitalize()} theme — click for {following}")
        if update:
            self.page.update()

    def _theme_mark(self, name: str, size: float = BUTTON_MARK) -> ft.Control:
        """A theme's face: a sun, a moon, or the Chad himself."""
        if name == "chad":
            return ft.Image(src=chad_mark(), width=size, height=size,
                            fit=ft.BoxFit.CONTAIN)
        return ft.Icon(ft.Icons.DARK_MODE if name == "dark" else ft.Icons.LIGHT_MODE)

    def _theme_name(self) -> str:
        """Which of the three is on screen."""
        if themes.is_chad(self.page):
            return "chad"
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        return "dark" if dark else "light"

    async def restore_theme(self) -> None:
        """Put back the theme this browser was last left in."""
        try:
            saved = await self.storage.get(THEME_KEY)
        except Exception:
            return
        if isinstance(saved, str) and saved in themes.NAMES:
            self._set_theme(saved, remember=False)

    async def _remember_theme(self, name: str) -> None:
        with contextlib.suppress(Exception):
            await self.storage.set(THEME_KEY, name)

    def _set_theme_button(self, mark: ft.Control, tooltip: str) -> None:
        self.theme_button.content = mark
        self.theme_button.tooltip = tooltip

    def _toggle_theme(self, _e: AnyEvent) -> None:
        """Cycle light -> dark -> Chad."""
        following = themes.NAMES.index(self._theme_name()) + 1
        self._set_theme(themes.NAMES[following % len(themes.NAMES)])

    def _set_theme(self, name: str, *, remember: bool = True) -> None:
        theme, mode = themes.theme_for(name)
        self.page.theme = theme
        self.page.theme_mode = mode
        self.page.bgcolor = themes.PAGE if name == "chad" else None
        if remember:
            with contextlib.suppress(Exception):
                self.page.run_task(self._remember_theme, name)
        self._sync_theme_button()
        self._rebuild_view()

    def explorer_base(self, pool: Pool) -> str:
        """The block explorer for the chain a pool is on, if it is known."""
        lite = self._lite_chains.get(pool.chain or self.chain)
        return lite.explorer if lite else ""

    def _rebuild_view(self) -> None:
        """Take on a theme that changed, everywhere -- not just on screen."""
        self.header.shadow = themes.bar_shadow(self.page)
        self.account_chip.border = themes.panel_border(self.page)
        self.connect_button.style = buttons.style(self.page)
        self.list_view.rebuild()
        self.portfolio_view.rebuild()
        if self._detail is not None:
            self.open_pool(self._detail.pool)
        self.page.update()

    # -- data -------------------------------------------------------------

    def _chain_changed(self, _e: AnyEvent) -> None:
        picked = self.chain_picker.value or DEFAULT_CHAIN
        if picked == self.chain:
            return
        self.chain = picked
        self.chain_picker.leading_icon = chain_icon(self.chain)
        self.show_list()
        self.page.run_task(self.load_pools)
        self.page.run_task(self.align_wallet_chain)

    async def align_wallet_chain(self) -> None:
        """Ask the wallet to move to the network being browsed."""
        wallet = self.wallet
        chain_id = await self.current_chain_id()
        if wallet is None or not chain_id or wallet.chain.chain_id == chain_id:
            return
        try:
            await wallet.provider.switch_chain(chain_id)
            return
        except RpcError as exc:
            if exc.code != UNKNOWN_CHAIN:
                self._say_chain(f"Your wallet stayed on {wallet.chain.name}: {exc.message}")
                return
        except WalletError as exc:
            self._say_chain(str(exc))
            return

        lite = (await self.api.lite_chains()).get(self.chain)
        if lite is None or not lite.rpc_url:
            self._say_chain(
                f"Your wallet does not know {chain_name(self.chain)}. Add the "
                "network there, then reload."
            )
            return
        try:
            await wallet.provider.add_chain(
                {
                    "chainId": hex(chain_id),
                    "chainName": lite.label,
                    "rpcUrls": [lite.rpc_url],
                    "blockExplorerUrls": [lite.explorer] if lite.explorer else [],
                    "nativeCurrency": {
                        "name": lite.native_symbol,
                        "symbol": lite.native_symbol,
                        "decimals": 18,
                    },
                }
            )
        except WalletError as exc:
            self._say_chain(f"Could not add {lite.label} to your wallet: {exc}")

    def _say_chain(self, message: str) -> None:
        self.error.value = message
        self.error.visible = True
        self.page.update()

    def _route_changed(self, event) -> None:
        self.page.run_task(self.apply_route, getattr(event, "route", None))

    async def _load_marks(self) -> None:
        """Fetch this chain's mark bundle, if the build has one."""
        if not http.is_browser():
            return
        from ui.assets import CHAINS, load_bundle, token_bundle
        from ui.logos import MARK_SIZE, pixel_ratio

        wanted = MARK_SIZE * pixel_ratio()
        chains = await load_bundle(CHAINS, wanted, http.get_bytes)
        if chains:
            self._draw_chain_marks()
        count = await load_bundle(token_bundle(self.chain), wanted, http.get_bytes)
        if count:
            print(f"marks: {count} for {self.chain} in one request")

        async def behind() -> None:
            """What is worth asking for once the rows are on screen."""
            if not chains and await load_bundle(CHAINS, wanted, http.get_bytes):
                print("marks: the network bundle landed on the second ask")
                self._draw_chain_marks()
            head = count or await load_bundle(
                token_bundle(self.chain), wanted, http.get_bytes
            )
            if not count and head:
                print(f"marks: {head} for {self.chain} on the second ask")
            more = await load_bundle(
                token_bundle(self.chain), wanted, http.get_bytes, rest=True
            )
            if more > head:
                print(f"marks: {more - head} more for {self.chain}, filled in behind")

        self._marks_rest = asyncio.create_task(behind())

    def _draw_chain_marks(self) -> None:
        """Build the picker again, now that its marks are in memory."""
        self._sync_chain_picker()
        safe_update(self.chain_picker)

    async def load_pools(self) -> None:
        """Point the list at a fresh feed for the current chain."""
        self._apply_layout()
        self.progress.visible = True
        self.error.visible = False
        self.page.update()
        marks = asyncio.create_task(self._load_marks())
        try:
            if not self.chains:
                self.chains = await self.api.chains()
                self._sync_chain_options()
            chain_id = self.chains.get(self.chain)
            if chain_id is None:
                raise ApiError(f"Curve's API does not cover {self.chain}.")
            lite = await self.api.is_lite(chain_id)
            self._lite_chains = await self.api.lite_chains()
            self.feed = PoolFeed(
                self.api,
                self.chain,
                chain_id,
                sort_by="tvl" if lite else DEFAULT_SORT,
                lite=lite,
            )
            self.list_view.attach(self.feed)
            self.page.update()
            await marks
            await self.list_view.load_more()
            totals = await self.api.chain_totals(chain_id)
            if not self._route_applied:
                self._route_applied = True
                self.page.run_task(self.apply_route, self.page.route)
        except ApiError as exc:
            marks.cancel()
            if rest := getattr(self, "_marks_rest", None):
                rest.cancel()
            self.error.value = str(exc)
            self.error.visible = True
            self.progress.visible = False
            self.page.update()
            return

        self._show_totals(totals)
        self.progress.visible = False
        self.page.update()

    def _show_totals(self, totals: dict) -> None:
        """The chain's two figures, on the bar and in the menu."""
        volume = totals.get("volume")
        self._totals = [("TVL", compact_usd(totals["tvl"] or 0.0))]
        if volume is not None:
            self._totals.append(("24h volume", compact_usd(volume)))
        self.totals.value = "   ·   ".join(
            f"{label} {value}" for label, value in self._totals
        )
        self.menu.items = self._menu_items()

    def _sync_chain_options(self) -> None:
        """Offer every chain the API reports, preferred ones first."""
        known = list(self.chains)
        ordered = [c for c in PREFERRED_CHAINS if c in known] + sorted(
            c for c in known if c not in PREFERRED_CHAINS
        )
        self._chain_order = ordered
        if self.chain not in known and ordered:
            self.chain = ordered[0]
        self._sync_chain_picker()

    # -- navigation -------------------------------------------------------

    def open_pool(self, pool: Pool) -> None:
        # Where Back goes. A pool opened from the portfolio belongs
        # to the portfolio: sending it to the pool list instead loses
        # the page the user was actually on, and the list is not even
        # where they were.
        self._opened_from = self._page_name
        self._detail = PoolDetailView(
            self.page,
            self.api,
            pool,
            self.contract_for,
            self.go_back,
            explorer=self.explorer_base(pool),
        )
        if self.page.width:
            self._detail.set_layout(layout_for(self.page.width))
        self._show(self._detail)
        self._go(routing.build(pool.chain or self.chain, pool.address))
        self.page.update()
        self.page.run_task(self._detail.load)

    def go_back(self) -> None:
        """Leave a pool page for whichever page it was opened from."""
        if self._opened_from == PAGE_PORTFOLIO:
            self.show_portfolio(reload=False)
        else:
            self.show_list()

    def open_holding(self, holding: portfolio.Holding) -> None:
        """A portfolio row names a pool; opening it needs the pool itself."""
        self.page.run_task(self.open_pool_by_address, holding.address)

    def show_list(self) -> None:
        self._detail = None
        self._page_name = PAGE_POOLS
        self._sync_nav()
        self._show(self.list_view)
        self._go(routing.build(self.chain))
        self.page.update()

    # -- portfolio --------------------------------------------------------

    def show_portfolio(self, *, reload: bool = True) -> None:
        """Open the portfolio page and start filling it in."""
        self._detail = None
        self._page_name = PAGE_PORTFOLIO
        self._sync_nav()
        self._show(self.portfolio_view)
        if self.page.width:
            self.portfolio_view.set_layout(layout_for(self.page.width))
        self._go(routing.build(self.chain, page=PAGE_PORTFOLIO))
        self.page.update()
        if reload:
            self.page.run_task(self.load_portfolio)

    def reader(self, chain_id: int, provider: Any) -> Any:
        """A wallet's provider with the public endpoints beside it."""
        if not chain_id:
            return provider
        wallet = self.wallet
        return FallbackProvider(
            provider,
            self.public_node(chain_id),
            spares_first=prefers_public_reads(provider),
            read_primary=wallet is None or wallet.chain.chain_id == chain_id,
        )

    async def current_chain_id(self) -> int:
        """The id of the network being browsed, waiting for the list if it
        has not arrived yet.
        """
        if not self.chains:
            with contextlib.suppress(ApiError):
                self.chains = await self.api.chains()
                self._sync_chain_options()
        return self.chains.get(self.chain) or 0

    def public_node(self, chain_id: int) -> PublicNode:
        node = self._public_nodes.get(chain_id)
        if node is None:
            node = PublicNode(chain_id, self._chainlist)
            self._public_nodes[chain_id] = node
        return node

    async def load_portfolio(self) -> None:
        """Remembered rows first, then those refreshed, then everything."""
        view = self.portfolio_view
        wallet = self.wallet
        self._earnings = []
        self._earning_seeds = None
        view.forget_earnings()
        if wallet is None or not wallet.address:
            view.say("Connect a wallet to see what it holds.")
            return
        account = wallet.address
        chain_id = await self.current_chain_id()

        remembered = await self._remembered_portfolio(account)
        if remembered:
            view.show(remembered)

        provider = self.reader(chain_id, wallet.provider)
        self.loading(0.0)
        try:
            if remembered:
                view.show(await portfolio.scan(
                    provider, portfolio.targets_for(remembered), account
                ))
            targets = await self.api.portfolio_targets(self.chain, chain_id)
            self.loading(PORTFOLIO_DISCOVERY_SHARE)
            holdings = await portfolio.scan(
                provider,
                targets,
                account,
                on_progress=lambda done, total: self.loading(
                    PORTFOLIO_DISCOVERY_SHARE
                    + (1 - PORTFOLIO_DISCOVERY_SHARE) * (done / max(total, 1))
                ),
            )
        except (WalletError, ApiError) as exc:
            if not remembered:
                view.say(f"Could not read this chain: {exc}")
            self.loaded()
            return

        self.loaded()
        if holdings:
            view.show(holdings)
        else:
            view.say(f"No deposits in any {chain_name(self.chain)} pool.")
        self.page.run_task(self._remember_portfolio, holdings, account)
        self.page.run_task(self.load_earnings, holdings, account, chain_id, provider)

    async def load_earnings(self, holdings, account: str, chain_id: int, provider) -> None:
        """What each position earns, and what it has earned but not taken."""
        staked = [h for h in holdings if h.gauge and h.staked > 0]
        if not staked:
            self._earning_seeds = None
            return

        try:
            rates = await self.api.pool_rates(
                chain_id, [holding.address for holding in staked]
            )
        except ApiError:
            rates = {}

        seeds: list[earnings.Earning] = []
        token_meta: dict[str, tuple[str, int, float]] = {}
        for holding in staked:
            seed = earnings.Earning(
                pool=holding.address,
                gauge=holding.gauge,
                staked=holding.staked,
                wallet=holding.wallet,
            )
            detail = rates.get(holding.address.lower())
            if detail is not None:
                seed, meta = earnings.seed_from_detail(seed, detail)
                token_meta.update(meta)
            seeds.append(seed)

        crv_price = 0.0
        entry = rewards.REWARDS.get(chain_id)
        if entry is not None:
            with contextlib.suppress(ApiError):
                crv_price = await self.api.usd_price(self.chain, entry.crv)
        self._earning_seeds = (seeds, token_meta, crv_price, chain_id)
        await self.reread_earnings(account, provider)

    async def reread_earnings(self, account: str, provider) -> None:
        """Ask the chain again what is owed, on the seeds already gathered."""
        if self._earning_seeds is None:
            return
        seeds, token_meta, crv_price, chain_id = self._earning_seeds
        try:
            filled = await earnings.read_earnings(
                provider, account, seeds,
                crv_price=crv_price, token_meta=token_meta,
            )
        except WalletError as exc:
            self.portfolio_view.claiming(
                f"Could not read what these gauges owe: {exc}", status.FAILED
            )
            return
        self._earnings = filled
        self.portfolio_view.show_earnings(filled, chain_id)

    async def wallet_is_here(self, chain_id: int) -> bool:
        """Is the wallet on the network the page is showing?

        The portfolio *reads* through public nodes pinned to the chain
        being browsed -- see `reader` -- so a wallet on another network is
        the normal state here rather than an edge case, and nothing else
        on this page would notice. A claim is the one thing that goes to
        the wallet, and the minter it names exists at that address on
        Ethereum and nowhere else: sent from a wallet on Arbitrum it costs
        gas and claims nothing, and the transaction can still succeed --
        an address with no code accepts calldata and returns.

        A wallet that cannot say where it is answers True: the pool panel
        makes the same choice, and refusing to act on an unreadable answer
        would be worse than letting the wallet refuse.
        """
        wallet = self.wallet
        if wallet is None or not chain_id:
            return False
        try:
            return await wallet.provider.chain_id() == chain_id
        except WalletError:
            return True

    async def claim_portfolio(self, crv: bool) -> None:
        """Claim one half of what the portfolio is owed."""
        view = self.portfolio_view
        wallet = self.wallet
        if wallet is None or not wallet.address:
            view.claiming("Connect a wallet first.", status.FAILED)
            return
        chain_id = self.chains.get(self.chain) or 0
        if not await self.wallet_is_here(chain_id):
            view.claiming(
                f"Your wallet is on another network. Switch it to "
                f"{chain_name(self.chain)} to claim.",
                status.FAILED,
            )
            return
        plan = earnings.claim_plan(chain_id, self._earnings)
        count = len(plan.crv) if crv else (1 if plan.extras else 0)
        if not count:
            view.claiming("Nothing to claim.", status.FAILED)
            return
        what = "CRV" if crv else "rewards"
        view.claiming(
            f"Confirm {count} transaction{'s' if count > 1 else ''} in your wallet…"
        )
        try:
            sent = await earnings.send_claims(
                wallet.provider, wallet.address, plan, crv=crv
            )
            for index, tx in enumerate(sent, start=1):
                view.claiming(f"Waiting for {index}/{len(sent)}: {tx[:14]}…")
                await wait_for_confirmation(wallet.provider, tx)
        except WalletError as exc:
            view.claiming(
                "" if exc.rejected_by_user else str(exc), status.FAILED
            )
            return
        view.claiming(f"Claimed {what}.", status.DONE)
        await self.reread_earnings(wallet.address, wallet.provider)

    def loading(self, fraction: float | None = None) -> None:
        """Show the strip under the top bar, at `fraction` or indefinite."""
        self.progress.value = fraction
        self.progress.visible = True
        safe_update(self.progress)

    def loaded(self) -> None:
        self.progress.visible = False
        safe_update(self.progress)

    async def _remembered_portfolio(self, account: str) -> list[portfolio.Holding]:
        with contextlib.suppress(Exception):
            saved = await self.storage.get(PORTFOLIO_KEY)
            if isinstance(saved, str) and saved:
                return portfolio.from_json(json.loads(saved), account, self.chain)
        return []

    async def _remember_portfolio(
        self, holdings: list[portfolio.Holding], account: str
    ) -> None:
        with contextlib.suppress(Exception):
            await self.storage.set(
                PORTFOLIO_KEY,
                json.dumps(portfolio.to_json(holdings, account, self.chain)),
            )

    # -- the address bar ---------------------------------------------------
    # On web this is the browser's URL: a pool page has an address worth
    # sending to someone, and Back means what it looks like it means.

    def _go(self, route: str) -> None:
        """Push a route, unless the browser is already showing it."""
        if (self.page.route or "") != route:
            self.page.run_task(self.page.push_route, route)

    async def apply_route(self, raw: str | None) -> None:
        """Show whatever the address bar says. Safe to call repeatedly."""
        route = routing.parse(raw)
        if route.chain and route.chain != self.chain and route.chain in self.chains:
            self.chain = route.chain
            self.chain_picker.value = route.chain
            self.chain_picker.leading_icon = chain_icon(route.chain)
            self.show_list()
            await self.load_pools()

        if route.is_portfolio:
            if self._page_name != PAGE_PORTFOLIO:
                self.show_portfolio()
            return

        if not route.is_pool:
            if self._detail is not None or self._page_name != PAGE_POOLS:
                self.show_list()
            return

        open_already = self._detail is not None and routing.same_pool(
            route.pool, self._detail.pool.address
        )
        if open_already:
            return
        await self.open_pool_by_address(route.pool)

    async def open_pool_by_address(self, address: str) -> None:
        """Open a pool named in a URL, fetching it if need be."""
        chain_id = self.chains.get(self.chain)
        if not chain_id:
            return
        self.progress.visible = True
        self.page.update()
        try:
            pool = await self.api.get_pool(chain_id, address, self.chain)
        except ApiError as exc:
            self.error.value = str(exc)
            self.error.visible = True
            self.show_list()
            return
        finally:
            self.progress.visible = False
            self.page.update()
        self.open_pool(pool)

    def contract_for(self) -> PoolContract | None:
        """The open pool, bound to the best provider available."""
        if self._detail is None:
            return None
        pool = self._detail.pool
        if self.wallet is not None:
            return PoolContract(
                self.reader(pool.chain_id, self.wallet.provider),
                pool,
                self.wallet.address,
            )
        if not pool.chain_id:
            return None
        return PoolContract(self.public_node(pool.chain_id), pool, "")

    # -- wallet -----------------------------------------------------------

    def _show_account(self, *, expanded: bool = False) -> None:
        """Draw the connected address, short or in full."""
        wallet = self.wallet
        self._address_expanded = expanded and wallet is not None
        self.account_chip.visible = wallet is not None
        if wallet is None:
            self.account_label.value = ""
            self.account_chip.tooltip = None
            return
        self.account_label.value = wallet.address if expanded else wallet.short_address
        self.account_chip.tooltip = f"{wallet.name}  ·  {wallet.chain.name}"

    def _nav_link(self, label: str, page: str) -> ft.Control:
        """One page link: as big as a pool name, and it says where you are."""
        here = self._page_name == page
        return ft.Container(
            ft.Text(
                label,
                size=ROW_TITLE,
                weight=ft.FontWeight.BOLD,
                color=ft.Colors.ON_SURFACE if here else ft.Colors.ON_SURFACE_VARIANT,
            ),
            on_click=lambda _e, target=page: self.go_page(target),
            ink=True,
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            border=ft.Border(bottom=ft.BorderSide(2, ft.Colors.PRIMARY))
            if here
            else None,
        )

    def _sync_nav(self) -> None:
        """Redraw the links -- and the menu, which is the links on a phone."""
        self.menu.items = self._menu_items()
        self.nav.content = ft.Row(
            [
                self._nav_link("Pools", PAGE_POOLS),
                self._nav_link("Portfolio", PAGE_PORTFOLIO),
            ],
            spacing=NAV_SPACING,
            tight=True,
        )

    def _brand_hovered(self, e: ft.Event[ft.Container]) -> None:
        """Slide the pages out from under the mark, over the totals."""
        if self.menu.visible:
            return  # narrow: the menu button is the way in
        self.nav.width = NAV_WIDTH if e.data else 0
        self.totals.opacity = 0.0 if e.data else 1.0
        self.page.update()

    def go_page(self, page: str) -> None:
        """Switch pages, through the URL so that Back works."""
        if page == PAGE_PORTFOLIO:
            self.show_portfolio()
        else:
            self.show_list()

    def _account_hovered(self, e: ft.Event[ft.Container]) -> None:
        """Grow to the full address under the cursor, shrink on the way out."""
        room = not self.page.width or self.page.width >= ADDRESS_EXPAND_MIN_PAGE
        self._show_account(expanded=bool(e.data) and room)
        self.page.update()

    def _wallet_clicked(self, _e: ft.Event[ft.Container]) -> None:
        if self.wallet is not None:
            self.page.show_dialog(self._wallet_dialog(self.wallet))

    def _connect_ended(self, previous: Wallet | None) -> None:
        """Put the header back after a connect that did not happen."""
        self.connect_button.content = CONNECT_LABEL
        self.connect_button.disabled = False
        self.connect_button.visible = previous is None
        self._show_account(expanded=self._address_expanded)
        self.page.update()

    async def connect(
        self, _e: AnyEvent | None, *, always_choose: bool = False
    ) -> None:
        self.connect_button.disabled = True
        self.connect_button.content = "Connecting…"
        self.error.visible = False
        self.page.update()
        previous = self.wallet
        try:
            self.wallet = await Wallet.connect(
                choose=self._choose_wallet, always_choose=always_choose
            )
        except WalletError as exc:
            if not exc.rejected_by_user:
                self.error.value = str(exc)
                self.error.visible = True
            self._connect_ended(previous)
            return

        if previous is not None and previous is not self.wallet:
            with contextlib.suppress(WalletError):
                await previous.close()

        self.connect_button.content = CONNECT_LABEL
        self.connect_button.disabled = False
        self.connect_button.visible = False
        self._show_account()
        self.page.update()
        await self.align_wallet_chain()
        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        if self._detail is not None:
            await self._detail.refresh_actions()
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    async def restore(self) -> None:
        """Pick up the previous session, silently, or leave things as they are."""
        try:
            wallet = await Wallet.restore()
        except WalletError:
            return
        if wallet is None:
            return
        self.wallet = wallet
        self.connect_button.visible = False
        self._show_account()
        self.page.update()
        await self.align_wallet_chain()
        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        if self._detail is not None:
            await self._detail.refresh_actions()
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    async def _wallet_changed(self) -> None:
        """The wallet switched account or network behind our back."""
        if self.wallet is None:
            return
        self._show_account(expanded=self._address_expanded)
        self.page.update()
        if await self._follow_wallet_chain():
            return
        if self._detail is not None:
            await self._detail.refresh_actions()
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    async def _follow_wallet_chain(self) -> bool:
        """Point the app at the network the wallet moved to."""
        wallet = self.wallet
        if wallet is None:
            return False
        name = next(
            (n for n, i in self.chains.items() if i == wallet.chain.chain_id), ""
        )
        if not name:
            self.error.value = (
                f"Your wallet is on {wallet.chain.name}, which Curve's API does"
                f" not cover. Pools shown are on {chain_name(self.chain)}."
            )
            self.error.visible = True
            self.page.update()
            return False
        if name == self.chain:
            return False
        self.chain = name
        self.chain_picker.value = name
        self.chain_picker.leading_icon = chain_icon(name)
        self.show_list()
        await self.load_pools()
        return True

    async def _wallet_gone(self) -> None:
        self.wallet = None
        self._show_account()
        self.connect_button.visible = True
        self.connect_button.disabled = False
        self.page.update()
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    # -- the wallet panel -------------------------------------------------

    def _wallet_dialog(self, wallet: Wallet) -> ft.AlertDialog:
        """The full address, and the two things you can do to a connection."""
        note = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT)

        async def copy(_e: AnyEvent) -> None:
            await self.page.clipboard.set(wallet.address)
            note.value = "Address copied."
            self.page.update()

        def change(_e: AnyEvent) -> None:
            self.page.pop_dialog()
            self.page.run_task(self._change_wallet)

        def disconnect(_e: AnyEvent) -> None:
            self.page.pop_dialog()
            self.page.run_task(self._disconnect_wallet)

        actions: list[ft.Control] = [ft.TextButton("Copy", on_click=copy)]
        if is_browser():
            actions.append(ft.TextButton("Change wallet", on_click=change))
        actions += [
            ft.TextButton("Disconnect", on_click=disconnect),
            ft.TextButton("Close", on_click=lambda _e: self.page.pop_dialog()),
        ]

        return ft.AlertDialog(
            title=ft.Row(
                [
                    wallet_mark(wallet.icon, wallet.name, 30),
                    ft.Text(wallet.name or "Wallet"),
                ],
                spacing=12,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Column(
                [
                    ft.Text("ADDRESS", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Text(wallet.address, size=BODY, selectable=True),
                    ft.Text(
                        f"On {wallet.chain.name}"
                        + ("" if is_browser() else "  ·  switch account in the wallet"),
                        size=SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    *self._transport_note(),
                    note,
                ],
                tight=True,
                spacing=6,
            ),
            actions=actions,
        )

    def _transport_note(self) -> list[ft.Control]:
        """Explain a browser window that cannot reach browser wallets."""
        if is_browser() or not self.page.web:
            return []
        return [
            ft.Text(
                "Python is running on this machine, so only the local "
                "wallet is reachable. Publish the app (flet publish) for "
                "browser wallets and WalletConnect.",
                size=LABEL,
                color=ft.Colors.ON_SURFACE_VARIANT,
            )
        ]

    async def _change_wallet(self) -> None:
        """Connect a different wallet, keeping this one until that works."""
        await self.connect(None, always_choose=True)

    async def _disconnect_wallet(self) -> None:
        if self.wallet is not None:
            with contextlib.suppress(WalletError):
                await self.wallet.disconnect()
        await self._wallet_gone()
        if self._detail is not None:
            await self._detail.refresh_actions()

    async def _choose_wallet(self, options: list[WalletChoice]) -> str | None:
        """Ask which wallet to use when the browser announced several."""
        chosen: dict[str, str | None] = {"uuid": None}
        finished = asyncio.Event()

        def pick(uuid: str | None) -> None:
            chosen["uuid"] = uuid
            finished.set()
            self.page.pop_dialog()

        def _cancel() -> None:
            """Dismissed without choosing -- the dialog is already closing."""
            chosen["uuid"] = None
            finished.set()

        dialog = ft.AlertDialog(
            title=ft.Text("Choose a wallet"),
            content=ft.Column(
                [
                    ft.ListTile(
                        leading=wallet_mark(option.icon, option.name),
                        title=ft.Text(option.name),
                        on_click=lambda _e, u=option.uuid: pick(u),
                    )
                    for option in options
                ],
                tight=True,
                spacing=4,
            ),
            actions=[ft.TextButton("Cancel", on_click=lambda _e: pick(None))],
            on_dismiss=lambda _e: _cancel(),
        )
        self.page.show_dialog(dialog)
        await finished.wait()
        return chosen["uuid"]


def main(page: ft.Page) -> None:
    CurveApp(page)


# Guarded so the module can be imported without launching -- which is what
# Flet's host-mode tests do to drive the app in-process.
if __name__ == "__main__":
    ft.run(main)
