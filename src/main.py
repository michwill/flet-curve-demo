"""An alternative Curve UI, written in Python with Flet.

    flet run src/main.py       desktop -> Frame / qeth on 127.0.0.1:1248
    flet publish src/main.py   browser -> MetaMask / Rabby / WalletConnect

The pool list and the charts are read-only and need no wallet at all, so
the app is fully usable before anyone connects one; connecting only turns
on the deposit/withdraw/swap/stake panel. That split is deliberate -- an
app that demands a wallet to show you a table is asking for a signature it
does not need yet.

The `wallet` package here is the one from flet-pay-example, unchanged: it
is the EIP-1193 seam that makes the same Python run in a browser worker and
on the desktop. Everything Curve-specific lives in `curve/`, which imports
no Flet, and `ui/`, which imports no network code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any

import flet as ft

from curve import ApiError, CurveApi, Pool, PoolContract, earnings, portfolio, rewards
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
#: v2 covers 12 chains against v1's 21 -- see docs/curve-api.md -- so the
#: real list is read from `/pools/chains/` rather than hardcoded.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")

#: Where the progress bar sits when the pool list has arrived and the
#: balances have not been read yet. The two take about the same time.
PORTFOLIO_DISCOVERY_SHARE = 0.5

# -- opening somewhere other than the front page ------------------------
#
# Two knobs for driving the app without driving the *UI*: which page to
# open on, and which theme to open in. They exist for looking at it --
# every visual check otherwise starts with hovering a logo and clicking
# through to the page in question, and on the desktop build there is no
# address bar to shortcut that with.
#
# Environment rather than argv, because `flet run` owns the command line
# and passes nothing through:
#
#     CURVE_ROUTE=/ethereum/portfolio CURVE_THEME=chad flet run src/main.py
#
# Both are ignored when unset, so a normal launch is unchanged.
ROUTE_ENV = "CURVE_ROUTE"
THEME_ENV = "CURVE_THEME"
#: And how big to open the window, as WIDTHxHEIGHT. Resizing an open
#: window to a phone's width is not the same test: the app then lays out
#: twice and what is on screen depends on which half of the startup the
#: resize lands in. `CURVE_WINDOW=390x844` opens narrow to begin with.
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


#: Where the last portfolio scan is remembered, so the page has something
#: to show while the next one runs.
PORTFOLIO_KEY = "flet-curve.portfolio"

#: Where the chosen theme is remembered, per browser or per desktop
#: install. Namespaced, because shared preferences are shared.
THEME_KEY = "flet-curve.theme"

#: What a wallet answers when it has never heard of the network being asked
#: for (EIP-3085). Anything else is a refusal, or a wallet that cannot
#: switch at all, and neither is worth a second attempt.
UNKNOWN_CHAIN = 4902

#: The Curve mark, sized against the wordmark beside it rather than against
#: the header's height -- the two read as one lockup.
BRAND_LOGO = 34
#: Below this the header has no room to give, so hovering does nothing --
#: a chip that grew here would push the chain picker off the row.
#:
#: A question about the *window*, which the window can answer, rather than
#: about text, which it cannot. The chip itself is given no width at all;
#: see where it is built.
ADDRESS_EXPAND_MIN_PAGE = 1100

#: The two pages the app has, as they appear in the URL and the nav.
PAGE_POOLS = "pools"
PAGE_PORTFOLIO = "portfolio"

#: Room around a page, inside the scroller rather than around it: the
#: scrollbar is drawn at the edge of the thing that scrolls, so padding
#: the scroller itself would move the bar in off the window's edge.
BODY_PADDING = 20

#: Space between the wordmark and the chain totals. Wider than the gap
#: *inside* the totals -- "TVL … · 24h volume …" separates its two halves
#: with three spaces around an interpunct -- because otherwise the TVL
#: reads as belonging to the word Curve rather than to the line it is in.
TOTALS_GAP = 22

#: Space between the mark and the first link. It lives *inside* the nav
#: rather than between the two, so it slides in with the links instead of
#: sitting there as a permanent gap after the wordmark.
NAV_GAP = 26

#: Space between the links themselves. They are bold and pool-name sized
#: now, so they need more air between them than a caption would.
NAV_SPACING = 14

#: How wide the nav slides open: the gap, plus both links. A Row inside a
#: clipped Container has no width of its own to animate from, so this is
#: measured rather than derived.
NAV_WIDTH = NAV_GAP + NAV_SPACING + 210

#: Below this the header has no room to slide anything open, so the mark
#: becomes a menu button instead. Same threshold the address chip uses --
#: they are competing for the same row.
NAV_EXPAND_MIN_PAGE = 900

#: What the browser tab and the desktop window are called. Not "Flet"
#: anywhere: that is how it is built, which is of interest to whoever is
#: reading the source and to nobody looking at the page.
APP_TITLE = "Curve Finance"

#: The connect button's resting label. It is swapped rather than blanked
#: while connecting: a `Button` with an `icon` and no `content` refuses to
#: render at all.
CONNECT_LABEL = "Connect wallet"

# -- a header that fits a phone -----------------------------------------
#
# It did not. At 390px the chain picker's name, the connect button's label
# and the theme button add up to more than the bar has, so they ran off the
# right-hand edge -- "Conne" and then nothing. Below the card breakpoint
# every one of them loses its words:
#
#   * the picker keeps its network mark and drops the name (the menu it
#     opens still spells them out, which is where you are choosing);
#   * connect becomes the wallet icon -- `ui.buttons.StandIn`;
#   * the theme moves into the menu the mark already opens, where it can
#     name all three rather than cycling through them a tap at a time.
#
# Width when it is only marks: leading icon, its inset and the arrow.
CHAIN_PICKER_WIDTH = 185
CHAIN_PICKER_NARROW_WIDTH = 78

#: How wide the *open* menu is, whatever the closed field has shrunk to.
#:
#: A `Dropdown`'s menu takes the field's width unless it is given one of
#: its own, and on a phone the field is a bare mark 78px wide. So the menu
#: was 78px too, and every name in it was cropped away: a column of
#: unlabelled circles, which is no way to pick a network and not what
#: `_chain_option` is written to draw. The field is the only thing short
#: of room on that header -- the menu opens over the whole page.
#:
#: Sized for the longest name offered ("Polygon zkEVM") beside its mark,
#: with room to spare for a chain the API names and this app does not.
CHAIN_MENU_WIDTH = 200

#: How big a theme's face is drawn on the button, and in the menu it moves
#: into on a phone.
BUTTON_MARK = 22
MENU_MARK = 20

#: The network mark inside the picker. Smaller than a token mark elsewhere:
#: a dense dropdown's field is barely taller than its text.
CHAIN_ICON = 14


def chain_icon(chain: str) -> ft.Control | None:
    """The selected network's mark, inset from the field's border.

    `Dropdown.leading_icon` goes straight into the decoration box with no
    padding of its own, so the mark touches the left border however small
    it is -- shrinking it alone just puts a smaller logo against the
    border. The inset has to come from the mark itself.
    """
    mark = chain_mark(chain, CHAIN_ICON)
    if mark is None:
        return None
    return ft.Container(mark, padding=ft.Padding.only(left=10, right=4))


def wallet_mark(icon: str | None, name: str, size: float = 28) -> ft.Control:
    """The wallet software's own face, or a generic wallet glyph.

    EIP-6963 requires a wallet to announce an icon, so this is usually the
    real thing; WalletConnect announces none -- it is a protocol, not a
    wallet -- and gets the one this app bundles. Two cases have no art at
    all and get the glyph: a pre-EIP-6963 wallet that only sets
    `window.ethereum`, and the desktop endpoint, which is Frame or qeth
    with no way to tell which. A letter was worse than useless there -- the
    desktop provider is called "Frame / qeth (127.0.0.1:1248)", so it drew
    a large "F" for a wallet that may well be neither.
    """
    fallback = ft.CircleAvatar(
        content=ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET, size=size * 0.6),
        radius=size / 2,
    )
    if not icon:
        return fallback
    # No `cache_width` here, unlike the token logos: wallet art arrives as a
    # `data:` URI that is usually SVG, which has no pixel size to decode at
    # and fails outright when asked for one.
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
        #: Public JSON-RPC, for reading a pool with no wallet connected.
        #: The directory is shared and fetched lazily -- a session that
        #: connects a wallet never asks chainlist for anything.
        #: Has the URL the page was opened with been acted on yet?
        self._route_applied = False
        self._chainlist = ChainlistDirectory()
        self._public_nodes: dict[int, PublicNode] = {}
        self.chain = DEFAULT_CHAIN
        self.chains: dict[str, int] = {}
        #: Lite deployments by chain name, once the list has loaded. Only
        #: they publish an explorer URL; everything else is a table.
        self._lite_chains: dict[str, LiteChain] = {}
        self.feed: PoolFeed | None = None
        self._detail: PoolDetailView | None = None
        #: Which of the two pages is showing. The detail view is a state
        #: of whichever page opened it rather than a page of its own,
        #: which is what `_opened_from` remembers for the Back button.
        self._page_name = PAGE_POOLS
        self._opened_from = PAGE_POOLS
        self._address_expanded = False

        # Key-value storage: the browser's on web, a file on the desktop.
        # Constructing it registers it with the page -- `page.shared_
        # preferences` does the same thing and is on its way out.
        self.storage = ft.SharedPreferences()

        self._build()
        # A theme asked for on the command line wins over the remembered
        # one, and skips reading storage at all.
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
            # Reconnect to the wallet used last time -- but only if it is
            # still authorised, which `Wallet.restore` checks without
            # prompting. A page that opens a wallet dialog by itself is
            # worse than one that shows a Connect button.
            #
            # Not when `autoconnect` said no because the user disconnected:
            # that is a decision, and there is nothing remembered to
            # restore from anyway.
            page.run_task(self.restore)

    async def dress_window(self) -> None:
        """Put the Curve mark on the desktop window, where that is possible.

        `flet run` starts a prebuilt Flutter host that sets no window icon
        of its own, and Flet's `window.icon` is Windows-only, so on X11
        this is done by hand -- see `ui.window_icon`. Imported here rather
        than at module scope because it touches `ctypes`, which the browser
        build has no business loading.

        Retried briefly: the window is usually up by the time a session
        starts, but "usually" is not "always", and there is nothing to set
        the property on until it is.
        """
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
        # Light and dark are Material's, seeded; Chad is a hand-set
        # palette -- see `ui/theme.py`. Which one is on decides more than
        # colour: it also decides whether panels get a hard shadow.
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = themes.material()
        page.dark_theme = themes.material()
        page.window.width, page.window.height = startup_window() or (1280.0, 900.0)

        #: Is the header down to icons? Read by the connect button's
        #: wrapper, its stand-in and the menu, so setting it in
        #: `_apply_layout` is the whole of the switch.
        self._icons = False
        #: The picker's networks, in the order it offers them. Kept so its
        #: options can be rebuilt when the names have to go.
        self._chain_order: list[str] = list(PREFERRED_CHAINS)
        #: The chain's headline figures, as (label, value) pairs. Kept
        #: apart from the line they are written on because the phone puts
        #: them somewhere else entirely -- see `_menu_items`.
        self._totals: list[tuple[str, str]] = []

        self.chain_picker = ft.Dropdown(
            options=[self._chain_option(c) for c in PREFERRED_CHAINS],
            # Replaced by the API's own list once `load_pools` has run.
            value=self.chain,
            width=CHAIN_PICKER_WIDTH,
            # The one width that does not follow the header in and out of
            # icons: you are reading names here whatever the field shows.
            menu_width=CHAIN_MENU_WIDTH,
            dense=True,
            # Material's own default, said out loud because the narrow
            # header turns it off and "off" needs something to go back to.
            border=ft.InputBorder.OUTLINE,
            leading_icon=chain_icon(self.chain),
            on_select=self._chain_changed,
        )
        self.totals = ft.Text(
            "",
            size=SMALL,
            color=ft.Colors.ON_SURFACE_VARIANT,
            no_wrap=True,
            # Faded rather than removed when the nav slides over it: a
            # control that vanishes takes the row's layout with it.
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
                # Mipmapped, as the token marks are -- see `ui.logos`.
                filter_quality=ft.FilterQuality.MEDIUM,
                # If the compiled assets are missing, the wordmark stands in.
                error_content=ft.Text("CURVE", size=TITLE, weight=ft.FontWeight.BOLD),
            )
            if logo
            else ft.Text("CURVE", key="brand", size=TITLE, weight=ft.FontWeight.BOLD)
        )
        # The pages, revealed by hovering the mark. See `_brand_hovered`.
        self.nav = ft.Container(
            width=0,
            padding=ft.Padding.only(left=NAV_GAP),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            animate=ft.Animation(
                duration=ft.Duration(milliseconds=160),
                curve=ft.AnimationCurve.EASE_OUT,
            ),
        )
        # On a narrow page the mark is a menu button instead: there is no
        # room to slide anything open, and a tap has to be enough.
        self.menu = ft.PopupMenuButton(
            icon=ft.Icons.MENU,
            visible=False,
            tooltip="Pages",
        )
        # After the menu, because it fills that too -- the two say the same
        # thing at different widths.
        self._sync_nav()

        # The wordmark sits beside the mark as if it were part of it. The
        # build kind is still worth knowing, but only when you go looking.
        self.build_label = ft.Text(
            "Curve",
            size=TITLE,
            weight=ft.FontWeight.BOLD,
            tooltip=f"{'browser' if is_browser() else 'desktop'} build",
        )
        self.account_label = ft.Text("", size=SMALL, no_wrap=True)
        # The address is a control, not a caption: hovering it gives you all
        # 42 characters, clicking it opens the wallet's own panel.
        #
        # **No width.** It had two, one per state, picked by measuring an
        # address in the browser's Roboto and adding a margin. That is a
        # guess about how wide text is, and a guess about text is wrong
        # somewhere: the desktop app draws in a different font and clipped
        # the tail of the address against the 42-character width -- half a
        # character short, which reads as a rendering fault rather than a
        # layout one. The same guess is what `ui.metrics` was deleted for.
        #
        # So the box takes its width from the address in it, and `…abcd`
        # and the full 42 characters are simply two different widths of
        # the same control. `animate_size` is what keeps the hover a
        # growth rather than a jump: it animates *to* the content's size,
        # which is the whole point -- nothing here has to know what that
        # size is, on any platform, in any font.
        self.account_chip = ft.Container(
            self.account_label,
            visible=False,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            # Outlined under Chad, like everything else there; in light
            # and dark it goes back to being separated by tone alone.
            border=themes.panel_border(page),
            border_radius=8,
            alignment=ft.Alignment.CENTER_LEFT,
            ink=True,
            on_click=self._wallet_clicked,
            on_hover=self._account_hovered,
            # Not `animate`: that animates properties this control is
            # given, and it is no longer given a width. This animates the
            # box to whatever its content turns out to measure.
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
            # A button draws no border of its own; the style is where one
            # goes, along with Chad's corners -- and `Themed` re-reads it
            # every update, because the header is built before the
            # remembered theme has been applied and outlives every switch
            # after that.
        )
        #: What is actually on the bar: the wallet mark, at every width.
        #:
        #: There was a labelled button here on a wide window, and it never
        #: sat right beside the network picker -- a 33px pill against a
        #: 46px field. Material will not be talked out of that: a `padding`
        #: given as a `Padding` is ignored, an integer moves the width once
        #: the minimum height binds, and a Row cannot stretch a child with
        #: no bounded height to stretch into. Every way of matching the two
        #: came down to picking a number and hoping, which is the thing
        #: this app does not do with sizes.
        #:
        #: So there is no frame to match. An icon has no height to disagree
        #: about, and it is what the theme button and the phone header were
        #: already doing -- the bar now reads as marks either side of the
        #: one framed control on it, which is the network you are browsing.
        #:
        #: `connect_button` stays as the thing this follows: `visible` and
        #: `disabled` are set on it from a dozen places that know nothing
        #: about layout, and `ft.Button` will not render with an icon and
        #: no label anyway. It is simply no longer drawn.
        self.connect_icon = buttons.StandIn(
            self.connect_button,
            lambda: True,
            icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
            tooltip=CONNECT_LABEL,
            on_click=self.connect,
        )
        # A Container rather than an IconButton: one of the three states
        # is drawn with an image (the wireframe mark), and an IconButton
        # takes only an icon. The same reason the sortable column headings
        # are Containers -- and, as there, `ink` keeps the press visible.
        self.theme_button = ft.Container(
            on_click=self._toggle_theme,
            ink=True,
            border_radius=22,
            padding=8,
            alignment=ft.Alignment.CENTER,
        )
        self._sync_theme_button()
        page.on_platform_brightness_change = lambda _e: self._sync_theme_button(update=True)
        # One source of responsive truth: every view is told the layout,
        # nobody measures anything for itself.
        page.on_resize = self._resized

        # The mark and the wordmark are one hover target: sliding the nav
        # open from either half of a lockup that reads as one thing.
        brand = ft.Container(
            ft.Row([self.brand, self.build_label], spacing=8, tight=True),
            on_click=lambda _e: self.go_page(PAGE_POOLS),
            ink=True,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=4, vertical=2),
        )
        # **The hover target is the mark, the links, and the space the
        # links slide over.** With it on the mark alone, moving the
        # pointer onto a link left the mark and closed the very thing
        # being reached for; with it on the mark and links only, the
        # totals beside them were dead space in the middle of a gesture
        # that is about to cover them anyway.
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
        )
        # The bar spans the window; what is written on it does not. Past
        # `MAX_CONTENT_WIDTH` this box stops growing and centres, so the
        # brand stays over the first column of the table and the wallet
        # over the last, instead of the two drifting to opposite edges of
        # a monitor while the table sits in the middle. See `_apply_width`.
        self._header_box = ft.Container(
            ft.Row(
                [
                    self.menu,
                    lockup,
                    # The wallet, in one place whether or not there is one.
                    # These three are one slot in three states -- the
                    # address, the button that gets you an address, and
                    # that button with no room for its label -- so they sit
                    # together, left of the network. They used to straddle
                    # it: connect on the right, and the address it produced
                    # on the left, so the one thing that moved when you
                    # connected was the thing you had just clicked.
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
            padding=ft.Padding.symmetric(horizontal=20, vertical=10),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
            # Chad only, and hard-edged, like the panels below it.
            shadow=themes.bar_shadow(page),
        )

        self.list_view = PoolListView(page, on_open=self.open_pool)
        self.portfolio_view = PortfolioView(
            page, on_open=self.open_holding, on_claim=self.claim_portfolio
        )
        #: The portfolio's earnings, and what they were read from. The
        #: seeds are the half that comes from the API and does not change
        #: when a claim lands, so a claim re-reads the chain and nothing
        #: else -- see `reread_earnings`.
        self._earnings: list[earnings.Earning] = []
        self._earning_seeds: tuple | None = None
        self.progress = ft.ProgressBar(visible=False)
        self.error = ft.Text("", size=SMALL, color=ft.Colors.ERROR, visible=False)
        # One slot that holds either the list or a detail page. Simpler than
        # Flet's view stack and behaves the same on both platforms.
        #
        # **This is what scrolls, and it is the whole width of the window.**
        # A scrollbar is drawn by whatever scrolls, at that thing's own
        # edge -- `ft.Scrollbar` only styles it, there is no way to draw one
        # somewhere else -- so a list that scrolled inside its bordered card
        # put the bar inside the card, twenty pixels in from the window and
        # again on the pool page two thirds of the way across. One scroller,
        # spanning the window, puts one bar where a bar belongs. The padding
        # that used to be on this container moves inside it, so the content
        # still stands clear of the edges and the bar has the margin to
        # itself.
        #
        # The header above it does not scroll: it is the navigation, and on
        # a phone it is the *only* navigation.
        #: Which page the body is holding. Read by the scroll handler,
        #: which is the window's and so belongs to no page in particular.
        self._showing: ft.Control = self.list_view
        # A `ListView` rather than a scrolling `Column`: a Column with
        # `scroll` set and one very tall child does not scroll here at all
        # -- it clips, silently, with no overflow reported -- while a
        # ListView holding the same child scrolls the way the pool list's
        # own used to.
        # The page itself, and the box that decides how wide it is allowed
        # to be. One box that outlives every page rather than a fresh one
        # per `_show`, so the width set on a resize survives opening a pool.
        self._page_box = ft.Container(self.list_view, padding=BODY_PADDING, expand=True)
        self.body = ft.ListView(
            controls=[
                ft.Row([self._page_box], alignment=ft.MainAxisAlignment.CENTER, spacing=0)
            ],
            expand=True,
            on_scroll=self._body_scrolled,
            # Throttled, as the list's own scroll handler was: without it
            # the handler fires on every frame of a fling and queues a page
            # request per frame.
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
        """Put a page in the body, at the top of it.

        Scrolled back to the top, because the body is the scroller now:
        opening a pool from row two hundred would otherwise open it two
        hundred rows down its own page.

        The padding is on `_page_box` rather than on the body so that the
        scrollbar keeps to the window's edge: it is drawn at the edge of
        whatever scrolls, and padding the scroller would push it inwards by
        exactly as much. Swapping the box's content rather than replacing
        the box keeps the width `_apply_width` gave it.
        """
        self._page_box.content = view
        self._showing = view
        self.page.run_task(self._to_top)

    async def _to_top(self) -> None:
        """Back to the top of the page. Harmless before it is mounted."""
        with contextlib.suppress(Exception):
            await self.body.scroll_to(offset=0)

    def _body_scrolled(self, e: ft.OnScrollEvent) -> None:
        """The page scrolled. Only the pool list wants to know.

        It pages as it goes, and the scroller it used to listen to was its
        own; now there is one for the window and it belongs to nobody in
        particular, so it is handed on.
        """
        if self._showing is self.list_view:
            self.list_view.page_scrolled(e)

    def _menu_items(self) -> list[ft.PopupMenuItem]:
        """What the mark opens.

        The pages always, ticked to say which one you are on. On a phone,
        everything else the header has had to give up as well: the themes
        under them, and the chain's two figures at the bottom.

        The theme button cycles light -> dark -> Chad, which is fine when
        it is on screen showing you where you are. Folded into a menu it
        would be a mystery tap, so here the three are named and the one you
        are in is ticked.
        """
        # Ticked where the wide header underlines: the menu is the only
        # thing saying which page you are on once the links are gone.
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
        # An item with no content is a divider.
        items: list[ft.PopupMenuItem] = [*pages, ft.PopupMenuItem()]
        items += [
            ft.PopupMenuItem(
                # The same face the button shows, so the menu and the bar
                # are recognisably about the same thing. `icon` takes a
                # control as well as an icon name, which is what lets Chad
                # be himself rather than a stand-in glyph.
                icon=self._theme_mark(name, MENU_MARK),
                content=ft.Text(f"{name.capitalize()} theme"),
                checked=name == current,
                on_click=lambda _e, chosen=name: self._set_theme(chosen),
            )
            for name in themes.NAMES
        ]
        # Last, and disabled. They are what the header says on a wider
        # window, and they are figures rather than somewhere to go -- so
        # they sit under the things that are, where Material's greying
        # reads as "not a destination" instead of as "not ready yet".
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
        # Options are rebuilt, so the selection is set again after them.
        self.chain_picker.value = self.chain
        self.chain_picker.width = (
            CHAIN_PICKER_WIDTH if labelled else CHAIN_PICKER_NARROW_WIDTH
        )
        # No outline once it is a mark and an arrow. A form field's border
        # is there to say "there is a value in here to change"; around a
        # bare icon it says nothing and only draws a box on a bar that has
        # no room for boxes -- the wallet beside it is an icon with no
        # frame either.
        self.chain_picker.border = (
            ft.InputBorder.OUTLINE if labelled else ft.InputBorder.NONE
        )
        self.chain_picker.leading_icon = chain_icon(self.chain)

    def _resized(self, e: ft.PageResizeEvent) -> None:
        self._apply_layout(e.width)

    def _apply_width(self, width: float) -> None:
        """Stop the page growing once it is wider than it wants to be.

        Two boxes -- the header's contents and the page below it -- either
        fill the window or take `MAX_CONTENT_WIDTH` and centre. `expand`
        and `width` are the same switch and are set together: a box that
        expands ignores a width, and one given a width must stop expanding
        or it will fill the row again.

        Only the outer boxes are told anything. What is inside them still
        lays itself out from its own content, at whatever width it is
        handed -- which is why this does not need to know about tables,
        charts or action panels, and why it is the same two lines whatever
        page is open.
        """
        capped = content_width(width)
        for box in (self._header_box, self._page_box):
            box.width = capped
            box.expand = capped is None

    def _apply_layout(self, width: float | None = None) -> None:
        """Push the current layout at every view.

        Also called on startup: `on_resize` fires on *changes*, so a window
        that opens narrow would otherwise never be told it is narrow.
        """
        width = width or self.page.width or 0
        if not width:
            return
        # How many device pixels a logical one is worth, which is what
        # decides the tier each mark is drawn from. Read here rather than
        # once at startup because `page.media` is not always answered by
        # the first paint, and a window moved between displays changes it.
        # See `ui.logos.set_pixel_ratio`.
        media = getattr(self.page, "media", None)
        logos.set_pixel_ratio(getattr(media, "device_pixel_ratio", None))
        self._apply_width(width)
        layout = layout_for(width)
        # The chain totals are the first thing to go: a phone header has
        # room for the chain and the wallet, and nothing else.
        self.totals.visible = not layout.cards
        self.build_label.visible = not layout.cards
        # And then the words go too. `_icons` is read by the connect
        # button's wrapper and its stand-in, so setting it is all that
        # swaps them.
        icons = layout.cards
        if icons != self._icons:
            self._icons = icons
            self._sync_chain_picker()
        self.theme_button.visible = not icons
        self.menu.items = self._menu_items()
        # Narrow: the mark becomes a menu button, because there is no room
        # to slide a nav open over a header this full.
        narrow = width < NAV_EXPAND_MIN_PAGE
        self.menu.visible = narrow
        if narrow:
            self.nav.width = 0
            self.totals.opacity = 1.0
        # The views repaint themselves -- `set_layout` ends in an update --
        # and the header does not, so it has to be told. Without this the
        # picker was still spelling out "Ethereum" at 390px, and the theme
        # button was still on the row, because nothing had asked the bar to
        # redraw: opening narrow worked (the first paint takes everything)
        # and *reaching* narrow did not.
        safe_update(self.header)
        # And the body for the same reason: the page's own view redraws
        # itself below, but the box holding it is not part of any view, so
        # a width set on it stays on the Python side until something asks
        # the scroller to repaint. Without this the header centred on a
        # wide window and the table under it stayed stretched.
        safe_update(self.body)
        self.list_view.set_layout(layout)
        self.portfolio_view.set_layout(layout)
        if self._detail is not None:
            self._detail.set_layout(layout)

    def _chain_option(self, chain: str, labelled: bool = True) -> ft.DropdownOption:
        """A network's mark beside its proper name, not its API slug.

        `content` is what the open menu draws and `text` is what the closed
        field shows, which is the whole trick on a phone: dropping `text`
        leaves the field showing its mark alone, while the menu still names
        every network.
        """
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

        The other reading -- showing the theme the click will get you --
        is what this did first, and it is unreadable: a moon while the
        screen is plainly light says the opposite of what is true. The
        tooltip carries the destination instead, where there is room to
        say it in words.
        """
        current = self._theme_name()
        mark = self._theme_mark(current)
        following = themes.NAMES[(themes.NAMES.index(current) + 1) % len(themes.NAMES)]
        self._set_theme_button(mark, f"{current.capitalize()} theme — click for {following}")
        if update:
            self.page.update()

    def _theme_mark(self, name: str, size: float = BUTTON_MARK) -> ft.Control:
        """A theme's face: a sun, a moon, or the Chad himself.

        Shared by the button and the menu so the two cannot drift -- the
        menu is where the button goes on a phone, and a different picture
        for the same theme in the two places would read as two settings.
        """
        if name == "chad":
            return ft.Image(src=chad_mark(), width=size, height=size,
                            fit=ft.BoxFit.CONTAIN)
        return ft.Icon(ft.Icons.DARK_MODE if name == "dark" else ft.Icons.LIGHT_MODE)

    def _theme_name(self) -> str:
        """Which of the three is on screen.

        Derived rather than remembered: the theme can also change under
        the app, when the desktop's own brightness flips while the mode is
        still SYSTEM.
        """
        if themes.is_chad(self.page):
            return "chad"
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        return "dark" if dark else "light"

    async def restore_theme(self) -> None:
        """Put back the theme this browser was last left in.

        Asynchronous because the storage is: on web it is the browser's,
        reached over the same channel as everything else, so the first
        paint happens in the default theme and this arrives just after.

        Silent about failure: storage can be unavailable (a private
        window, a desktop with no writable state directory), and the
        answer then is the system's own light or dark, which is what the
        app did before it had a third theme at all.
        """
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
        """Cycle light -> dark -> Chad.

        Chad changes shape as well as colour -- its panels carry a hard
        shadow that the Material themes must not -- so the views are
        rebuilt rather than repainted.
        """
        following = themes.NAMES.index(self._theme_name()) + 1
        self._set_theme(themes.NAMES[following % len(themes.NAMES)])

    def _set_theme(self, name: str, *, remember: bool = True) -> None:
        theme, mode = themes.theme_for(name)
        self.page.theme = theme
        self.page.theme_mode = mode
        # Chad puts white panels on a grey page, which is the shape of the
        # site it comes from. Material's own themes leave the page to the
        # scheme, so this is set back to None for them.
        self.page.bgcolor = themes.PAGE if name == "chad" else None
        if remember:
            # Survives a reload, which matters more now that a pool page
            # has a URL somebody might open twice. Fired and not waited
            # for: the theme is already on screen, and whether the write
            # landed changes nothing until the next load.
            with contextlib.suppress(Exception):
                self.page.run_task(self._remember_theme, name)
        self._sync_theme_button()
        # Shadows are set when a control is built, so the view has to be
        # built again for them to appear or go.
        self._rebuild_view()

    def explorer_base(self, pool: Pool) -> str:
        """The block explorer for the chain a pool is on, if it is known.

        A Lite chain says which one it uses, and that answer is better
        than any table -- those are the chains a table would be wrong
        about. Empty otherwise, which `curve.explorers` reads as "use the
        table".
        """
        lite = self._lite_chains.get(pool.chain or self.chain)
        return lite.explorer if lite else ""

    def _rebuild_view(self) -> None:
        """Take on a theme that changed, everywhere -- not just on screen.

        The header is not re-made: it outlives every view, so it only
        needs what the new theme asks for. The list is not re-made either,
        but it *is* told, whether or not it is the view showing. It
        outlives a pool page, and one built under another theme keeps that
        theme's shadow, border and hover until something rebuilds it --
        so switching theme on a pool page and pressing Back landed on a
        stale list. Found by the stateful tests, which is exactly the kind
        of ordering no single-transition test looks at.
        """
        self.header.shadow = themes.bar_shadow(self.page)
        self.account_chip.border = themes.panel_border(self.page)
        self.connect_button.style = buttons.style(self.page)
        # *Both* tables, whichever is showing. The portfolio is built once
        # at startup, when the theme is still whatever the app opened in,
        # and the saved theme arrives later -- so left untold it kept the
        # first theme's header band, border, shadow and hover while the
        # pool list wore the new one. Two tables that are meant to be the
        # same table.
        self.list_view.rebuild()
        self.portfolio_view.rebuild()
        if self._detail is not None:
            self.open_pool(self._detail.pool)
        self.page.update()

    # -- data -------------------------------------------------------------

    def _chain_changed(self, _e: AnyEvent) -> None:
        picked = self.chain_picker.value or DEFAULT_CHAIN
        # A dropdown reports a *selection*, not a change, and picking the
        # network you are already on is a selection. Everything below
        # closes the pool page and reloads the list, so without this,
        # opening the picker on a pool and choosing where you already are
        # throws you back to the list -- an answer to a question the user
        # did not ask.
        if picked == self.chain:
            return
        self.chain = picked
        # The closed field carries the selected network's mark too, not
        # just the name.
        self.chain_picker.leading_icon = chain_icon(self.chain)
        self.show_list()
        self.page.run_task(self.load_pools)
        # And take the wallet with us. Reads go through the wallet's
        # provider, so browsing one network with a wallet on another
        # quotes addresses that hold no code there: every estimate comes
        # back empty and the panel can only say the pool did not answer.
        self.page.run_task(self.align_wallet_chain)

    async def align_wallet_chain(self) -> None:
        """Ask the wallet to move to the network being browsed.

        On a deliberate pick from the header, and on connecting or
        restoring -- anywhere the app and the wallet can end up naming
        different networks. It used to be the header alone, on the
        reasoning that the wallet's own network is a choice to follow
        rather than override; but the app only follows it on a *change*
        (`_follow_wallet_chain`), so a page that opened on Ethereum with a
        wallet already sitting on another chain followed nothing and said
        nothing. Reads went out through that wallet and came back empty.

        The wallet prompts; it may refuse, and refusing is a perfectly
        good answer that leaves the panels' own notice standing -- and
        now leaves the reads correct too, since `reader` stops asking a
        wallet that is somewhere else.

        A wallet that has never heard of the network answers 4902. For the
        Curve Lite chains that is the normal case, and their API is the
        only place that publishes what EIP-3085 needs to add one, so the
        offer is made with that.
        """
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

    async def load_pools(self) -> None:
        """Point the list at a fresh feed for the current chain.

        Only the first page is fetched here; the rest arrive as the list
        scrolls. See `curve.api.PoolFeed` for why paging beat loading
        everything up front.
        """
        self._apply_layout()
        self.progress.visible = True
        self.error.visible = False
        self.page.update()
        try:
            if not self.chains:
                self.chains = await self.api.chains()
                self._sync_chain_options()
            chain_id = self.chains.get(self.chain)
            if chain_id is None:
                raise ApiError(f"Curve's API does not cover {self.chain}.")
            # Curve Lite chains measure no trading, so the list opens on
            # TVL: sorting by a volume that is unknown everywhere would
            # order the page arbitrarily. See `curve.lite`.
            lite = await self.api.is_lite(chain_id)
            # Kept because the pool page needs it synchronously: a Lite
            # chain publishes its own block explorer, and the addresses on
            # that page link to it.
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
            await self.list_view.load_more()
            totals = await self.api.chain_totals(chain_id)
            if not self._route_applied:
                # Deep link: the URL the page was opened with. Done here
                # rather than at startup because it may name a chain, and
                # a chain is only checkable once the API has listed them.
                self._route_applied = True
                self.page.run_task(self.apply_route, self.page.route)
        except ApiError as exc:
            self.error.value = str(exc)
            self.error.visible = True
            self.progress.visible = False
            self.page.update()
            return

        self._show_totals(totals)
        self.progress.visible = False
        self.page.update()

    def _show_totals(self, totals: dict) -> None:
        """The chain's two figures, on the bar and in the menu.

        Both, because the header gives them up at card widths and the menu
        is where the rest of it went. They arrive a request after the menu
        was built, so setting them rebuilds it -- otherwise a phone's menu
        would carry the two lines it was built with, which is none.

        No volume clause on a Lite chain: nothing there counts trades, and
        "24h volume $0.00" would read as a quiet day rather than as a
        measurement nobody takes.
        """
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
        # Where Back goes. A pool opened from the portfolio belongs to the
        # portfolio: sending it to the pool list instead loses the page the
        # user was actually on, and the list is not even where they were.
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
        """A portfolio row names a pool; opening it needs the pool itself.

        The row carries an address and little else -- it was built from a
        balance, not from a listing -- so the pool page is reached the
        same way a deep link reaches it.
        """
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
        """Open the portfolio page and start filling it in.

        `reload` is false when coming *back* to it from a pool page: the
        rows are still there and still right, and rescanning a thousand
        pools because somebody pressed Back would be rude.
        """
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
        """A wallet's provider with the public endpoints beside it.

        An injected wallet is asked first: it is the user's own view of
        the chain and the place their transaction will run, and it is a
        node in the same browser. The public endpoints sit behind it, so
        a read it cannot carry costs a retry rather than the page.

        Over WalletConnect the order is the other way round, because
        there the "wallet" is a relay to a phone -- see
        `rpc.prefers_public_reads`. It is still in the list, still asked
        when nothing public answers, and still the only thing asked to
        sign.

        A wallet on *another network* is not asked at all. It would answer
        -- that is the trouble. Reads are `eth_call`, and a wallet on
        Fraxtal answers a question about an Ethereum address by looking up
        that address on Fraxtal, where there is no code: `0x`, successfully,
        which decodes to a zero balance. Nothing raises, so `FallbackProvider`
        has no reason to move on and the public node behind it is never
        reached. The portfolio then reports "No deposits in any Ethereum
        pool" for an account with eight positions, which is how this was
        found -- with the wallet's own UI insisting it was on Ethereum
        while `eth_chainId` said 252.

        `align_wallet_chain` asks it to come across, and a wallet that
        does lands here matching on the next read. This is what happens
        when it does not: the answer is still right, because the public
        node is pinned to the network being browsed. Only the wallet is
        allowed to be somewhere else.

        Without a chain id there is no public node to name, so the wallet
        is all there is and is returned unwrapped.
        """
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

        `restore` and `load_portfolio` both run at startup, racing
        `load_pools` for the one thing that turns a chain *name* into an
        id. Reading `self.chains` directly there gets `0` as often as not,
        and `0` is not a harmless miss: `reader` reads it as "no public
        node to name" and hands back the bare wallet -- the single reader
        this page must not be given, since a wallet on the wrong network
        answers every call successfully and wrongly.

        `CurveApi.chains` is cached, so this is one request the first time
        and none after.
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
        """Remembered rows first, then those refreshed, then everything.

        The order is the point: a scan is a second or two, and the answer
        is nearly always the same as last time, so there is no reason to
        show a blank page while proving it.
        """
        view = self.portfolio_view
        wallet = self.wallet
        # Before anything is drawn, and before the early returns: what the
        # last read found belongs to whichever account it was read for,
        # and this runs on connecting, switching account and switching
        # chain. A claim button still offering the previous wallet's CRV
        # is not a stale number, it is the wrong account's.
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
                # A handful of calls, so the numbers on screen stop being
                # last week's before the pool list has even arrived.
                view.show(await portfolio.scan(
                    provider, portfolio.targets_for(remembered), account
                ))
            # The two halves of the wait cost about the same, so the bar
            # splits at the middle: asking which pools exist, then asking
            # the chain about all of them.
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
        # A third read, after the rows are on screen. It is the slowest of
        # the three -- a pool payload per gauge -- and nothing on the page
        # needs to wait for it, so it fills the two columns in afterwards.
        self.page.run_task(self.load_earnings, holdings, account, chain_id, provider)

    async def load_earnings(self, holdings, account: str, chain_id: int, provider) -> None:
        """What each position earns, and what it has earned but not taken.

        Only the staked ones: an unstaked position has no gauge to ask and
        earns no rewards, so asking about it is a call that can only come
        back zero.
        """
        staked = [h for h in holdings if h.gauge and h.staked > 0]
        if not staked:
            self._earning_seeds = None
            return

        # One call, however many pools -- see `CurveApi.pool_rates`. This
        # used to be a request per staked pool, and on an address in three
        # hundred gauges that was three hundred requests for figures the
        # scan had already downloaded a moment earlier: the pool list it
        # pages to find the gauges carries every rate on the chain.
        try:
            rates = await self.api.pool_rates(
                chain_id, [holding.address for holding in staked]
            )
        except ApiError:
            # No rates is a page with no APR column, not a page with no
            # rewards: the claimable amounts come from the chain and are
            # read below regardless.
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
        # Kept, so that re-reading what is owed after a claim does not
        # mean asking the API for a rate that has not moved -- see
        # `reread_earnings`.
        self._earning_seeds = (seeds, token_meta, crv_price, chain_id)
        await self.reread_earnings(account, provider)

    async def reread_earnings(self, account: str, provider) -> None:
        """Ask the chain again what is owed, on the seeds already gathered.

        This is what runs after a claim confirms. Everything on the page
        that a claim changes -- the two buttons' amounts, the "Unclaimed
        rewards" total, the Rewards column -- is read from the gauges, and
        leaving it showing the figures the claim was made against says the
        claim did not happen.

        The pool payloads are not asked for again: a claim does not move an
        APR, and the whole point of re-reading here rather than reloading
        the page is that one is a Multicall3 round and the other is a scan.
        """
        if self._earning_seeds is None:
            return
        seeds, token_meta, crv_price, chain_id = self._earning_seeds
        try:
            filled = await earnings.read_earnings(
                provider, account, seeds,
                crv_price=crv_price, token_meta=token_meta,
            )
        except WalletError as exc:
            # This used to return, leaving both columns on their en dash
            # and no claim bar -- which is exactly what a portfolio with
            # nothing accruing looks like. The read failing and there
            # being nothing to read are different answers and the page
            # said the same thing for both.
            self.portfolio_view.claiming(
                f"Could not read what these gauges owe: {exc}", status.FAILED
            )
            return
        self._earnings = filled
        self.portfolio_view.show_earnings(filled, chain_id)

    async def claim_portfolio(self, crv: bool) -> None:
        """Claim one half of what the portfolio is owed.

        Two buttons and two code paths, because the chain offers two:
        CRV is minted for `msg.sender` and so goes through `mint_many`
        eight or thirty-two gauges at a time, while the incentives are
        transfers that name their recipient and so go through Multicall3
        in one send. The line says how many transactions are coming, so a
        second wallet prompt is expected rather than alarming.
        """
        view = self.portfolio_view
        wallet = self.wallet
        if wallet is None or not wallet.address:
            view.claiming("Connect a wallet first.", status.FAILED)
            return
        chain_id = self.chains.get(self.chain) or 0
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
            # A dismissed wallet prompt goes back to a blank line rather
            # than a red one -- see `WalletError.rejected_by_user`.
            view.claiming(
                "" if exc.rejected_by_user else str(exc), status.FAILED
            )
            return
        view.claiming(f"Claimed {what}.", status.DONE)
        # Not a reload. A claim moves reward tokens, not LP, so every
        # position on the page is exactly as it was -- what changed is
        # what the gauges owe, and that is one Multicall3 round rather
        # than a scan of every pool on the chain.
        await self.reread_earnings(wallet.address, wallet.provider)

    def loading(self, fraction: float | None = None) -> None:
        """Show the strip under the top bar, at `fraction` or indefinite.

        The same bar the pool list uses, in the same place: under the
        header and above whatever is loading. It sits in the page's own
        column, so showing it moves nothing -- a bar inside the page
        would push the table down as it appears and pull it back up as it
        goes.
        """
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

    # -- the address bar --------------------------------------------------
    #
    # On web this is the browser's URL: a pool page has an address worth
    # sending to someone, and Back means what it looks like it means.
    # `page.push_route` pushes a history entry and fires `on_route_change`,
    # which the browser also fires on Back -- so one handler serves both,
    # and it is written to be idempotent rather than to guess who called it.

    def _go(self, route: str) -> None:
        """Push a route, unless the browser is already showing it.

        Queued rather than awaited: `push_route` is a coroutine and every
        caller here is a click handler. That is what `page.go` did for
        itself before it was deprecated -- it is gone in Flet 0.90.
        """
        if (self.page.route or "") != route:
            self.page.run_task(self.page.push_route, route)

    async def apply_route(self, raw: str | None) -> None:
        """Show whatever the address bar says. Safe to call repeatedly.

        Called for the first URL and again on every route change, which
        includes the Back and Forward buttons -- there is no way to tell
        those apart, and no need to: this compares the route with what is
        on screen and moves only if they differ.
        """
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
        """Open a pool named in a URL, fetching it if need be.

        A link can name a pool that is nowhere in the loaded list -- below
        the TVL floor, or on page nine -- so this asks the API for that one
        pool rather than paging until it turns up.
        """
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
        """The open pool, bound to the best provider available.

        A wallet when there is one: it is the node that will execute the
        transaction, so a quote read through it is the quote least likely
        to surprise. Otherwise a public node, which can read but not sign
        -- rates are worth showing before anyone connects anything, and
        `get_dy` never needed an account. See `curve.rpc`.
        """
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
        """Draw the connected address, short or in full.

        The only place the chip's text is set, so the hover, a wallet-side
        account change and a fresh connection cannot disagree about what
        it says. Setting the text is the whole of it: the width follows
        from it rather than being chosen alongside it, which is what stops
        the two from ever disagreeing.
        """
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
        """One page link: as big as a pool name, and it says where you are.

        A `Container` rather than a `TextButton` for the reason the
        sortable column headings are -- a TextButton in this app hovers
        correctly and never fires its handler in the published web build.
        """
        here = self._page_name == page
        return ft.Container(
            ft.Text(
                label,
                size=ROW_TITLE,
                weight=ft.FontWeight.BOLD,
                # Full-strength ink for the page you are on, and the muted
                # one for the page you are not. Not the theme's primary:
                # under Chad that is a chocolate brown which reads as
                # decoration rather than as a link.
                color=ft.Colors.ON_SURFACE if here else ft.Colors.ON_SURFACE_VARIANT,
            ),
            on_click=lambda _e, target=page: self.go_page(target),
            ink=True,
            border_radius=6,
            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
            # And an underline under the current page, because colour alone
            # is a weak signal and not everyone sees it.
            border=ft.Border(bottom=ft.BorderSide(2, ft.Colors.PRIMARY))
            if here
            else None,
        )

    def _sync_nav(self) -> None:
        """Redraw the links -- and the menu, which is the links on a phone.

        Both say which page you are on, and both are wrong the moment it
        changes, so they are redrawn together rather than by whoever
        happens to remember.
        """
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
        """Slide the pages out from under the mark, over the totals.

        The same idea as the address chip: the header is full, so what is
        rarely wanted is hidden and what is hovered takes the room. The
        totals are the thing given up, which is why the nav overlays them
        rather than pushing them along -- a header that reflows under the
        cursor is hard to click.
        """
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
        """Grow to the full address under the cursor, shrink on the way out.

        Not on a narrow page: the header there has nothing to give, and a
        chip that grew would push the chain picker out of the row.
        """
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
        # Cancelling a "change wallet" leaves the session that was already
        # there: nothing was torn down to offer the picker, and the picker
        # changes what the bridge points at only once something is picked.
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
            # Dismissing the picker -- or the wallet's own connect prompt
            # -- is an answer, not a failure. Reporting it back tells the
            # user only what they just did, and in red it reads as though
            # something broke.
            if not exc.rejected_by_user:
                # In the header a failure would be clipped by the chip's
                # fixed width; the error line under it has the whole page.
                self.error.value = str(exc)
                self.error.visible = True
            self._connect_ended(previous)
            return

        if previous is not None and previous is not self.wallet:
            # Swapped, not disconnected: release the old transport without
            # recording an intent the user never expressed.
            with contextlib.suppress(WalletError):
                await previous.close()

        self.connect_button.content = CONNECT_LABEL
        self.connect_button.disabled = False
        self.connect_button.visible = False
        self._show_account()
        self.page.update()
        # After the header is drawn, because the wallet may prompt here and
        # a header still reading "Connecting…" behind that prompt describes
        # something that already finished.
        #
        # Before the handlers are on, so a wallet that does come across
        # does not also fire `_wallet_changed` into a second scan of the
        # chain this is about to read. `Wallet.chain` tracks the switch
        # either way -- the subscription is what the *app* hears, not what
        # the wallet knows.
        await self.align_wallet_chain()
        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        if self._detail is not None:
            await self._detail.refresh_actions()
        # Connecting *while on the portfolio* is the whole point of the
        # empty state it is showing, so read it now rather than making
        # the user find their way back to the page they are on.
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
        # Placed exactly as in `connect`, for the reasons given there. This
        # is the path the reported failure came in on: a remembered
        # session, a page opened on Ethereum, and a wallet still on
        # whatever it was last used for.
        await self.align_wallet_chain()
        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        if self._detail is not None:
            await self._detail.refresh_actions()
        # A portfolio opened by URL asks before the wallet is back --
        # restoring is asynchronous and deliberately quiet -- so it would
        # otherwise sit on "connect a wallet" with an address in the bar.
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    async def _wallet_changed(self) -> None:
        """The wallet switched account or network behind our back.

        Fired by `accountsChanged`/`chainChanged`, so this is the path for
        someone picking another account in MetaMask while the page is open.
        The chip keeps whatever state it was in -- collapsing it under the
        cursor would look like a glitch.
        """
        if self.wallet is None:
            return
        self._show_account(expanded=self._address_expanded)
        self.page.update()
        if await self._follow_wallet_chain():
            # The whole view was rebuilt for the new network, actions
            # included.
            return
        if self._detail is not None:
            await self._detail.refresh_actions()
        # Another account holds other things.
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    async def _follow_wallet_chain(self) -> bool:
        """Point the app at the network the wallet moved to. Did it move?

        A pool address is per-chain. Left alone, this page would go on
        offering Ethereum pools to a wallet now on Arbitrum, and the
        calldata would be signed against an address that is either nothing
        or somebody else's contract there. Following the wallet is what
        Curve's own site does, and it is the only version of this that
        cannot mis-send.

        Only on a *change*: at connect time the network you were browsing
        is a deliberate choice, and yanking the list out from under it
        would be rude.
        """
        wallet = self.wallet
        if wallet is None:
            return False
        name = next(
            (n for n, i in self.chains.items() if i == wallet.chain.chain_id), ""
        )
        if not name:
            # v2 covers 12 chains; a wallet can be on any of hundreds.
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
        # Whoever's positions those were, they are not on screen for a
        # page with no wallet behind it.
        if self._page_name == PAGE_PORTFOLIO:
            await self.load_portfolio()

    # -- the wallet panel -------------------------------------------------

    def _wallet_dialog(self, wallet: Wallet) -> ft.AlertDialog:
        """The full address, and the two things you can do to a connection.

        Changing wallet is a disconnect and a fresh connect rather than
        anything cleverer: EIP-1193 has no "switch account" call, and the
        picker only reappears once the current provider has let go.
        """
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

        # On the desktop there is one endpoint and no choice to offer: the
        # account is whichever one Frame or qeth has selected, and the app
        # now follows that by itself.
        actions: list[ft.Control] = [ft.TextButton("Copy", on_click=copy)]
        if is_browser():
            actions.append(ft.TextButton("Change wallet", on_click=change))
        actions += [
            ft.TextButton("Disconnect", on_click=disconnect),
            ft.TextButton("Close", on_click=lambda _e: self.page.pop_dialog()),
        ]

        return ft.AlertDialog(
            # The wallet's own icon, so the panel says which software you
            # are looking at before you read a word of it.
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
                    # Selectable so the address can be taken without the
                    # clipboard, which a browser may refuse.
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
        """Explain a browser window that cannot reach browser wallets.

        `flet run --web` runs Python on this machine and puts a Flutter
        client in a browser, so the page looks like the published app while
        the wallet layer is the desktop one: the local Frame/qeth endpoint,
        and nothing else. Extensions and WalletConnect live in the page and
        are unreachable from here, which is bewildering unless it is said
        out loud.
        """
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
        """Connect a different wallet, keeping this one until that works.

        The picker is forced open even when only one wallet answered:
        without that this command silently reconnects to the wallet you
        were already using and looks like it did nothing at all.

        The old session is *not* dropped first. It used to be, which meant
        cancelling the picker left you disconnected -- having asked only to
        look at the alternatives.
        """
        await self.connect(None, always_choose=True)

    async def _disconnect_wallet(self) -> None:
        if self.wallet is not None:
            # A wallet already gone on its own side is not a failure here:
            # the app's own state is what matters from this point.
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
# Flet's host-mode tests do to drive the app in-process. `flet run` and
# `flet publish` both execute this as `__main__` via runpy, so they are
# unaffected.
if __name__ == "__main__":
    ft.run(main)
