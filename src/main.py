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

import flet as ft

from curve import ApiError, CurveApi, Pool, PoolContract
from curve.api import PoolFeed
from curve.format import compact_usd
from curve.rpc import ChainlistDirectory, PublicNode
from curve.sort import DEFAULT_SORT
from ui import AnyEvent, routing
from ui import theme as themes
from ui.assets import chain_name, curve_logo, curve_wireframe
from ui.logos import chain_mark
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView
from ui.responsive import layout_for
from ui.typography import BODY, LABEL, SMALL, TITLE
from wallet import Wallet, WalletChoice, WalletError, autoconnect, is_browser
from wallet.base import RpcError

DEFAULT_CHAIN = "ethereum"
#: Shown first in the picker; anything else the API reports is appended.
#: v2 covers 12 chains against v1's 21 -- see docs/curve-api.md -- so the
#: real list is read from `/pools/chains/` rather than hardcoded.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")

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
#: How wide the address sits when it shows `0x1234…abcd` and when it shows
#: all 42 characters. Fixed so the hover can animate. The full width is
#: measured rather than guessed -- a checksummed address is ~316px in the
#: browser's Roboto at this size -- and then given a wide margin, because
#: Flutter's own metrics run a little wider than the browser's and a
#: clipped address is worse than a gap.
ADDRESS_SHORT_WIDTH = 128
ADDRESS_FULL_WIDTH = 380
#: Below this the header has no room to give, so hovering does nothing --
#: a chip that grew here would push the chain picker off the row.
ADDRESS_EXPAND_MIN_PAGE = 1100

#: The connect button's resting label. It is swapped rather than blanked
#: while connecting: a `Button` with an `icon` and no `content` refuses to
#: render at all.
CONNECT_LABEL = "Connect wallet"

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
        self.feed: PoolFeed | None = None
        self._detail: PoolDetailView | None = None
        self._address_expanded = False

        # Key-value storage: the browser's on web, a file on the desktop.
        # Constructing it registers it with the page -- `page.shared_
        # preferences` does the same thing and is on its way out.
        self.storage = ft.SharedPreferences()

        self._build()
        page.run_task(self.restore_theme)
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
        page.title = "Curve — Flet"
        page.padding = 0
        # Light and dark are Material's, seeded; Chad is a hand-set
        # palette -- see `ui/theme.py`. Which one is on decides more than
        # colour: it also decides whether panels get a hard shadow.
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = themes.material()
        page.dark_theme = themes.material()
        page.window.width = 1280
        page.window.height = 900

        self.chain_picker = ft.Dropdown(
            options=[self._chain_option(c) for c in PREFERRED_CHAINS],
            # Replaced by the API's own list once `load_pools` has run.
            value=self.chain,
            width=185,
            dense=True,
            leading_icon=chain_icon(self.chain),
            on_select=self._chain_changed,
        )
        self.totals = ft.Text(
            "", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True
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
                # If the compiled assets are missing, the wordmark stands in.
                error_content=ft.Text("CURVE", size=TITLE, weight=ft.FontWeight.BOLD),
            )
            if logo
            else ft.Text("CURVE", key="brand", size=TITLE, weight=ft.FontWeight.BOLD)
        )
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
        # 42 characters, clicking it opens the wallet's own panel. Fixed
        # widths rather than an intrinsic one so the growth can animate --
        # a Row cannot animate a child that sizes itself.
        self.account_chip = ft.Container(
            self.account_label,
            visible=False,
            width=ADDRESS_SHORT_WIDTH,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            alignment=ft.Alignment.CENTER_LEFT,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ink=True,
            on_click=self._wallet_clicked,
            on_hover=self._account_hovered,
            animate=ft.Animation(
                duration=ft.Duration(milliseconds=140),
                curve=ft.AnimationCurve.EASE_OUT,
            ),
        )
        self.connect_button = ft.Button(
            CONNECT_LABEL,
            icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
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

        header = ft.Container(
            ft.Row(
                [
                    self.brand,
                    self.build_label,
                    ft.Container(self.totals, expand=True),
                    self.account_chip,
                    # On the right, where the connected wallet used to
                    # repeat the network name back at you.
                    self.chain_picker,
                    self.connect_button,
                    self.theme_button,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=14,
            ),
            padding=ft.Padding.symmetric(horizontal=20, vertical=10),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )

        self.list_view = PoolListView(page, on_open=self.open_pool)
        self.progress = ft.ProgressBar(visible=False)
        self.error = ft.Text("", size=SMALL, color=ft.Colors.ERROR, visible=False)
        # One slot that holds either the list or a detail page. Simpler than
        # Flet's view stack and behaves the same on both platforms.
        self.body = ft.Container(self.list_view, expand=True, padding=20)

        page.add(
            ft.Column(
                [header, self.progress, self.error, self.body],
                spacing=0,
                expand=True,
            )
        )

    def _resized(self, e: ft.PageResizeEvent) -> None:
        self._apply_layout(e.width)

    def _apply_layout(self, width: float | None = None) -> None:
        """Push the current layout at every view.

        Also called on startup: `on_resize` fires on *changes*, so a window
        that opens narrow would otherwise never be told it is narrow.
        """
        width = width or self.page.width or 0
        if not width:
            return
        layout = layout_for(width)
        # The chain totals are the first thing to go: a phone header has
        # room for the chain and the wallet, and nothing else.
        self.totals.visible = not layout.cards
        self.build_label.visible = not layout.cards
        self.list_view.set_layout(layout)
        if self._detail is not None:
            self._detail.set_layout(layout)

    def _chain_option(self, chain: str) -> ft.DropdownOption:
        """A network's mark beside its proper name, not its API slug."""
        mark = chain_mark(chain)
        label = ft.Text(chain_name(chain), size=BODY)
        return ft.DropdownOption(
            key=chain,
            content=ft.Row([mark, label], spacing=8, tight=True) if mark else label,
            text=chain_name(chain),
        )

    def _sync_theme_button(self, update: bool = False) -> None:
        """Show what pressing it will get you: the *next* theme's mark.

        Three states now, so the icon names the destination rather than
        the state -- a moon while light, a sun while dark, and the
        wireframe mark for Chad.
        """
        if themes.is_chad(self.page):
            self._set_theme_button(ft.Icon(ft.Icons.LIGHT_MODE), "Light theme")
            if update:
                self.page.update()
            return
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        # From dark the next stop is Chad, which has a mark of its own.
        wireframe = curve_wireframe()
        if dark and wireframe:
            self._set_theme_button(
                ft.Image(src=wireframe, width=20, height=20, fit=ft.BoxFit.CONTAIN),
                "Chad theme",
            )
        elif dark:
            self._set_theme_button(ft.Icon(ft.Icons.LIGHT_MODE), "Light theme")
        else:
            self._set_theme_button(ft.Icon(ft.Icons.DARK_MODE), "Dark theme")
        if update:
            self.page.update()

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
        if themes.is_chad(self.page):
            self._set_theme("light")
            return
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        self._set_theme("chad" if dark else "dark")

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

    def _rebuild_view(self) -> None:
        """Re-make whichever view is on screen, in the current theme."""
        if self._detail is not None:
            self.open_pool(self._detail.pool)
        else:
            self.list_view.rebuild()
        self.page.update()

    # -- data -------------------------------------------------------------

    def _chain_changed(self, _e: AnyEvent) -> None:
        self.chain = self.chain_picker.value or DEFAULT_CHAIN
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

        Only on a deliberate pick from the header -- never on load, where
        the wallet's own network is a choice the app follows rather than
        overrides. The wallet prompts; it may refuse, and refusing is a
        perfectly good answer that leaves the panels' own notice standing.

        A wallet that has never heard of the network answers 4902. For the
        Curve Lite chains that is the normal case, and their API is the
        only place that publishes what EIP-3085 needs to add one, so the
        offer is made with that.
        """
        wallet = self.wallet
        chain_id = self.chains.get(self.chain)
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

        # No volume clause on a Lite chain: nothing there counts trades,
        # and "24h volume $0.00" would read as a quiet day rather than as
        # an absent measurement.
        volume = totals.get("volume")
        self.totals.value = f"TVL {compact_usd(totals['tvl'] or 0.0)}" + (
            f"   ·   24h volume {compact_usd(volume)}" if volume is not None else ""
        )
        self.progress.visible = False
        self.page.update()

    def _sync_chain_options(self) -> None:
        """Offer every chain the API reports, preferred ones first."""
        known = list(self.chains)
        ordered = [c for c in PREFERRED_CHAINS if c in known] + sorted(
            c for c in known if c not in PREFERRED_CHAINS
        )
        self.chain_picker.options = [self._chain_option(c) for c in ordered]
        if self.chain not in known and ordered:
            self.chain = ordered[0]
            self.chain_picker.value = self.chain
        self.chain_picker.leading_icon = chain_icon(self.chain)

    # -- navigation -------------------------------------------------------

    def open_pool(self, pool: Pool) -> None:
        self._detail = PoolDetailView(
            self.page, self.api, pool, self.contract_for, self.show_list
        )
        if self.page.width:
            self._detail.set_layout(layout_for(self.page.width))
        self.body.content = self._detail
        self._go(routing.build(pool.chain or self.chain, pool.address))
        self.page.update()
        self.page.run_task(self._detail.load)

    def show_list(self) -> None:
        self._detail = None
        self.body.content = self.list_view
        self._go(routing.build(self.chain))
        self.page.update()

    # -- the address bar --------------------------------------------------
    #
    # On web this is the browser's URL: a pool page has an address worth
    # sending to someone, and Back means what it looks like it means.
    # `page.go` pushes a history entry and fires `on_route_change`, which
    # the browser also fires on Back -- so one handler serves both, and it
    # is written to be idempotent rather than to guess who called it.

    def _go(self, route: str) -> None:
        """Push a route, unless the browser is already showing it."""
        if (self.page.route or "") != route:
            self.page.go(route)

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

        if not route.is_pool:
            if self._detail is not None:
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
            return PoolContract(self.wallet.provider, pool, self.wallet.address)
        if not pool.chain_id:
            return None
        node = self._public_nodes.get(pool.chain_id)
        if node is None:
            node = PublicNode(pool.chain_id, self._chainlist)
            self._public_nodes[pool.chain_id] = node
        return PoolContract(node, pool, "")

    # -- wallet -----------------------------------------------------------

    def _show_account(self, *, expanded: bool = False) -> None:
        """Draw the connected address, short or in full.

        The only place the chip's text and width are set, so the hover, a
        wallet-side account change and a fresh connection cannot disagree
        about what it says.
        """
        wallet = self.wallet
        self._address_expanded = expanded and wallet is not None
        self.account_chip.visible = wallet is not None
        self.account_chip.width = (
            ADDRESS_FULL_WIDTH if self._address_expanded else ADDRESS_SHORT_WIDTH
        )
        if wallet is None:
            self.account_label.value = ""
            self.account_chip.tooltip = None
            return
        self.account_label.value = wallet.address if expanded else wallet.short_address
        self.account_chip.tooltip = f"{wallet.name}  ·  {wallet.chain.name}"

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
            # In the header a failure would be clipped by the chip's fixed
            # width; the error line under it has the whole page.
            self.error.value = str(exc)
            self.error.visible = True
            self.connect_button.content = CONNECT_LABEL
            self.connect_button.disabled = False
            # Cancelling a "change wallet" leaves the session that was
            # already there: nothing was torn down to offer the picker, and
            # the picker changes what the bridge points at only once
            # something is picked.
            self.connect_button.visible = previous is None
            self._show_account(expanded=self._address_expanded)
            self.page.update()
            return

        if previous is not None and previous is not self.wallet:
            # Swapped, not disconnected: release the old transport without
            # recording an intent the user never expressed.
            with contextlib.suppress(WalletError):
                await previous.close()

        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        self.connect_button.content = CONNECT_LABEL
        self.connect_button.disabled = False
        self.connect_button.visible = False
        self._show_account()
        self.page.update()
        if self._detail is not None:
            await self._detail.refresh_actions()

    async def restore(self) -> None:
        """Pick up the previous session, silently, or leave things as they are."""
        try:
            wallet = await Wallet.restore()
        except WalletError:
            return
        if wallet is None:
            return
        self.wallet = wallet
        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        self.connect_button.visible = False
        self._show_account()
        self.page.update()
        if self._detail is not None:
            await self._detail.refresh_actions()

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
