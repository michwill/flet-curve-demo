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
from wallet import Wallet, WalletChoice, WalletError, autoconnect, is_browser

DEFAULT_CHAIN = "ethereum"
#: Shown first in the picker; anything else the API reports is appended.
#: v2 covers 12 chains against v1's 21 -- see docs/curve-api.md -- so the
#: real list is read from `/pools/chains/` rather than hardcoded.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")


class CurveApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.api = CurveApi()
        self.wallet: Wallet | None = None
        self.chain = DEFAULT_CHAIN
        self.chains: dict[str, int] = {}
        self.feed: PoolFeed | None = None
        self._detail: PoolDetailView | None = None

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
            leading_icon=chain_mark(self.chain),
            on_select=self._chain_changed,
        )
        self.totals = ft.Text(
            "", size=12, color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True
        )
        logo = curve_logo()
        self.brand = (
            ft.Image(
                key="brand",
                src=logo,
                width=26,
                height=26,
                fit=ft.BoxFit.CONTAIN,
                # If the compiled assets are missing, the wordmark stands in.
                error_content=ft.Text("CURVE", size=18, weight=ft.FontWeight.BOLD),
            )
            if logo
            else ft.Text("CURVE", key="brand", size=18, weight=ft.FontWeight.BOLD)
        )
        # The wordmark sits beside the mark as if it were part of it. The
        # build kind is still worth knowing, but only when you go looking.
        self.build_label = ft.Text(
            "Curve",
            size=18,
            weight=ft.FontWeight.BOLD,
            tooltip=f"{'browser' if is_browser() else 'desktop'} build",
        )
        self.account_label = ft.Text("", size=12)
        self.connect_button = ft.Button(
            "Connect wallet",
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
                    self.account_label,
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
        self.error = ft.Text("", size=12, color=ft.Colors.ERROR, visible=False)
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
        label = ft.Text(chain_name(chain), size=13)
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
        self.chain_picker.leading_icon = chain_mark(self.chain)
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
        self.chain_picker.leading_icon = chain_mark(self.chain)

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

    async def connect(self, _e: ft.ControlEvent | None) -> None:
        self.connect_button.disabled = True
        self.account_label.value = "Connecting…"
        self.page.update()
        try:
            self.wallet = await Wallet.connect(choose=self._choose_wallet)
        except WalletError as exc:
            self.account_label.value = str(exc)
            self.connect_button.disabled = False
            self.connect_button.visible = True
            self.page.update()
            return

        self.wallet.on_change(lambda: self.page.run_task(self._wallet_changed))
        self.wallet.on_disconnect(lambda: self.page.run_task(self._wallet_gone))
        self.account_label.value = self.wallet.short_address
        self.account_label.tooltip = self.wallet.chain.name
        self.connect_button.visible = False
        self.page.update()
        if self._detail is not None:
            await self._detail.refresh_actions()

    async def _wallet_changed(self) -> None:
        if self.wallet is None:
            return
        self.account_label.value = self.wallet.short_address
        self.account_label.tooltip = self.wallet.chain.name
        self.page.update()
        if self._detail is not None:
            await self._detail.refresh_actions()

    async def _wallet_gone(self) -> None:
        self.wallet = None
        self.account_label.value = ""
        self.connect_button.visible = True
        self.connect_button.disabled = False
        self.page.update()

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
                        leading=(
                            ft.Image(src=option.icon, width=28, height=28)
                            if option.icon
                            else ft.CircleAvatar(content=ft.Text(option.initial))
                        ),
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
