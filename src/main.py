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

import flet as ft

from curve import ApiError, CurveApi, Pool, PoolContract
from curve.api import PoolFeed
from curve.format import compact_usd
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView
from ui.assets import chain_name, curve_logo
from ui.logos import chain_mark
from ui.responsive import layout_for
from ui.typography import BODY, LABEL, SMALL, TITLE
from wallet import Wallet, WalletChoice, WalletError, autoconnect, is_browser

DEFAULT_CHAIN = "ethereum"
#: Shown first in the picker; anything else the API reports is appended.
#: v2 covers 12 chains against v1's 21 -- see docs/curve-api.md -- so the
#: real list is read from `/pools/chains/` rather than hardcoded.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")

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
    """The wallet software's own face, or its initial.

    EIP-6963 requires a wallet to announce an icon, so this is usually the
    real thing; WalletConnect announces none -- it is a protocol, not a
    wallet -- and gets the one this app bundles. Anything else falls back
    to a letter, which is also what a `data:` URI that fails to decode
    ends up as.
    """
    initial = (name or "?").strip()[:1].upper() or "?"
    fallback = ft.CircleAvatar(content=ft.Text(initial), radius=size / 2)
    if not icon:
        return fallback
    return ft.Image(
        src=icon,
        width=size,
        height=size,
        fit=ft.BoxFit.CONTAIN,
        cache_width=int(size * 3),
        filter_quality=ft.FilterQuality.MEDIUM,
        error_content=fallback,
    )


class CurveApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.api = CurveApi()
        self.wallet: Wallet | None = None
        self.chain = DEFAULT_CHAIN
        self.chains: dict[str, int] = {}
        self.feed: PoolFeed | None = None
        self._detail: PoolDetailView | None = None
        self._address_expanded = False

        self._build()
        page.run_task(self.load_pools)
        if autoconnect():
            self.connect_button.visible = False
            page.run_task(self.connect, None)

    # -- layout -----------------------------------------------------------

    def _build(self) -> None:
        page = self.page
        page.title = "Curve — Flet"
        page.padding = 0
        page.theme_mode = ft.ThemeMode.SYSTEM
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
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
        self.theme_button = ft.IconButton(on_click=self._toggle_theme)
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
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        self.theme_button.icon = ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE
        if update:
            self.page.update()

    def _toggle_theme(self, _e: ft.ControlEvent) -> None:
        dark = self.page.theme_mode == ft.ThemeMode.DARK or (
            self.page.theme_mode == ft.ThemeMode.SYSTEM
            and self.page.platform_brightness == ft.Brightness.DARK
        )
        self.page.theme_mode = ft.ThemeMode.LIGHT if dark else ft.ThemeMode.DARK
        self._sync_theme_button()
        self.page.update()

    # -- data -------------------------------------------------------------

    def _chain_changed(self, _e: ft.ControlEvent) -> None:
        self.chain = self.chain_picker.value or DEFAULT_CHAIN
        # The closed field carries the selected network's mark too, not
        # just the name.
        self.chain_picker.leading_icon = chain_icon(self.chain)
        self.show_list()
        self.page.run_task(self.load_pools)

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
                raise ApiError(f"Curve's v2 API does not cover {self.chain}.")
            self.feed = PoolFeed(self.api, self.chain, chain_id)
            self.list_view.attach(self.feed)
            self.page.update()
            await self.list_view.load_more()
            totals = await self.api.chain_totals(chain_id)
        except ApiError as exc:
            self.error.value = str(exc)
            self.error.visible = True
            self.progress.visible = False
            self.page.update()
            return

        self.totals.value = (
            f"TVL {compact_usd(totals['tvl'])}"
            f"   ·   24h volume {compact_usd(totals['volume'])}"
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
        self.page.update()
        self.page.run_task(self._detail.load)

    def show_list(self) -> None:
        self._detail = None
        self.body.content = self.list_view
        self.page.update()

    def contract_for(self) -> PoolContract | None:
        """The bound contract for the open pool, or None with no wallet."""
        if self.wallet is None or self._detail is None:
            return None
        return PoolContract(self.wallet.provider, self._detail.pool, self.wallet.address)

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

    async def connect(self, _e: ft.ControlEvent | None) -> None:
        self.connect_button.disabled = True
        self.connect_button.content = "Connecting…"
        self.error.visible = False
        self.page.update()
        try:
            self.wallet = await Wallet.connect(choose=self._choose_wallet)
        except WalletError as exc:
            # In the header a failure would be clipped by the chip's fixed
            # width; the error line under it has the whole page.
            self.error.value = str(exc)
            self.error.visible = True
            self.connect_button.content = CONNECT_LABEL
            self.connect_button.disabled = False
            self.connect_button.visible = True
            self.page.update()
            return

        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        self.connect_button.content = CONNECT_LABEL
        self.connect_button.disabled = False
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

        async def copy(_e: ft.ControlEvent) -> None:
            await self.page.clipboard.set(wallet.address)
            note.value = "Address copied."
            self.page.update()

        def change(_e: ft.ControlEvent) -> None:
            self.page.pop_dialog()
            self.page.run_task(self._change_wallet)

        def disconnect(_e: ft.ControlEvent) -> None:
            self.page.pop_dialog()
            self.page.run_task(self._disconnect_wallet)

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
                        f"On {wallet.chain.name}",
                        size=SMALL,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    note,
                ],
                tight=True,
                spacing=6,
            ),
            actions=[
                ft.TextButton("Copy", on_click=copy),
                ft.TextButton("Change wallet", on_click=change),
                ft.TextButton("Disconnect", on_click=disconnect),
                ft.TextButton("Close", on_click=lambda _e: self.page.pop_dialog()),
            ],
        )

    async def _change_wallet(self) -> None:
        """Drop the current connection, then connect again from scratch.

        `Wallet.connect` only offers the picker when the page announced
        more than one wallet -- with a single extension installed this is
        simply a reconnection, which is still the only way to reach another
        account in that wallet.
        """
        await self._disconnect_wallet()
        await self.connect(None)

    async def _disconnect_wallet(self) -> None:
        if self.wallet is not None:
            try:
                await self.wallet.disconnect()
            except WalletError:
                # Already gone wallet-side; the app's own state is what
                # matters from here.
                pass
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
            on_dismiss=lambda _e: (chosen.update(uuid=None), finished.set()),
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
