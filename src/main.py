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
from curve.format import compact_usd
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView
from wallet import Wallet, WalletChoice, WalletError, autoconnect, is_browser

DEFAULT_CHAIN = "ethereum"
#: Chains worth offering first. The rest are appended from `getPlatforms`,
#: which is authoritative -- see docs/curve-api.md.
PREFERRED_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")


class CurveApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.api = CurveApi()
        self.wallet: Wallet | None = None
        self.chain = DEFAULT_CHAIN
        self.pools: list[Pool] = []
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
            options=[ft.DropdownOption(key=c, text=c) for c in PREFERRED_CHAINS],
            value=self.chain,
            width=150,
            dense=True,
            on_select=self._chain_changed,
        )
        self.totals = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
        self.account_label = ft.Text("", size=12)
        self.connect_button = ft.Button(
            "Connect wallet",
            icon=ft.Icons.ACCOUNT_BALANCE_WALLET,
            on_click=self.connect,
        )
        self.theme_button = ft.IconButton(on_click=self._toggle_theme)
        self._sync_theme_button()
        page.on_platform_brightness_change = lambda _e: self._sync_theme_button(update=True)

        header = ft.Container(
            ft.Row(
                [
                    ft.Text("CURVE", size=18, weight=ft.FontWeight.BOLD),
                    ft.Text(
                        f"{'browser' if is_browser() else 'desktop'} build",
                        size=10,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    self.chain_picker,
                    ft.Container(self.totals, expand=True),
                    self.account_label,
                    self.connect_button,
                    self.theme_button,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=14,
            ),
            padding=ft.Padding.symmetric(horizontal=20, vertical=10),
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )

        self.list_view = PoolListView(on_open=self.open_pool)
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
        self.show_list()
        self.page.run_task(self.load_pools)

    async def load_pools(self) -> None:
        self.progress.visible = True
        self.error.visible = False
        self.page.update()
        try:
            self.pools = await self.api.pools(self.chain)
            totals = await self.api.chain_totals(self.chain)
        except ApiError as exc:
            self.error.value = str(exc)
            self.error.visible = True
            self.progress.visible = False
            self.page.update()
            return

        self.list_view.set_pools(self.pools)
        tvl = sum(p.tvl for p in self.pools)
        self.totals.value = (
            f"TVL {compact_usd(tvl)}   ·   24h volume {compact_usd(totals['volume'])}"
            f"   ·   crypto share {totals['crypto_share']:.2f}%"
        )
        self.progress.visible = False
        self.page.update()

    # -- navigation -------------------------------------------------------

    def open_pool(self, pool: Pool) -> None:
        self._detail = PoolDetailView(
            self.page, self.api, pool, self.contract_for, self.show_list
        )
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
        self.account_label.value = f"{self.wallet.short_address} · {self.wallet.chain.name}"
        self.connect_button.visible = False
        self.page.update()
        if self._detail is not None:
            await self._detail.refresh_actions()

    async def _wallet_changed(self) -> None:
        if self.wallet is None:
            return
        self.account_label.value = f"{self.wallet.short_address} · {self.wallet.chain.name}"
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


ft.run(main)
