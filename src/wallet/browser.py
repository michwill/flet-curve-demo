"""Browser transport: Pyodide worker <-> main thread <-> wallet connector.

This is the half that needs an explanation, because of one hard constraint:

    Flet runs your Python inside a **Web Worker**, and a Web Worker has no
    `window`. `window.ethereum` and every wallet connector (wagmi, RainbowKit,
    Web3Modal, EIP-6963 discovery) live on the **main thread**. Python
    therefore *cannot* reach the wallet directly, no matter what it imports.

So something has to carry calls across the worker boundary, and Flet's own
worker channel is not it -- that channel is Flet's private MessagePack
protocol between Dart and Python, and piggybacking on it means patching
Flet internals that will move under you.

The clean seam is `BroadcastChannel`: a standard same-origin message bus
available in *both* a Window and a Worker. Opening one on each side gives a
private channel that Flet neither knows nor cares about, needs zero patching
of the generated bundle, and survives Flet upgrades.

    Python (Pyodide, worker)          JS (main thread, assets/wallet_bridge.js)
    -------------------------         ----------------------------------------
    BrowserWalletProvider   <---- BroadcastChannel("flet-wallet") ---->  bridge
        .request(m, p)  --> {dir:"req", id, method, params}
                        <-- {dir:"res", id, result|error}
                        <-- {dir:"evt", event, data}          provider.request()
                                                                      |
                                                              EIP-6963 / wagmi
                                                                      |
                                                            MetaMask, Rabby, ...

Everything above `request()` is the same code the desktop build runs.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Callable

from . import settings
from .base import RpcError, WalletProvider, WalletUnavailable

#: Same string as in assets/wallet_bridge.js. Both sides must agree.
CHANNEL_NAME = "flet-wallet"
PROTOCOL_VERSION = 1

#: How long to wait for the main-thread bridge to answer a handshake.
HANDSHAKE_TIMEOUT = 10.0
#: Requests that wait on a human get no deadline at all.
REQUEST_TIMEOUT = 120.0
_INTERACTIVE = frozenset(
    {
        "eth_requestAccounts",
        "eth_sendTransaction",
        "personal_sign",
        "eth_signTypedData_v4",
        "wallet_addEthereumChain",
        "wallet_switchEthereumChain",
        "wallet_connect",
    }
)

#: Picking a wallet is interactive too, and not only for a click: the
#: WalletConnect connector opens a QR modal here and waits for the user to
#: scan it with a phone. That deserves far longer than a normal request --
#: but not *forever*. A connector that never settles (a dead relay, a modal
#: closed without rejecting its promise) would otherwise leave the UI
#: spinning on "Connecting..." with no way out. Three minutes is far more
#: than any real scan takes, and failing loudly beats hanging silently.
CONNECT_TIMEOUT = 180.0


def is_browser() -> bool:
    """True when this interpreter is Pyodide running in a browser."""
    return sys.platform == "emscripten"


class BrowserWalletProvider(WalletProvider):
    """EIP-1193 tunnelled over BroadcastChannel to a main-thread connector."""

    kind = "browser"

    def __init__(self) -> None:
        import js  # noqa: PLC0415  -- only importable inside Pyodide
        from pyodide.ffi import create_proxy  # noqa: PLC0415

        self._js = js
        self._channel = js.BroadcastChannel.new(CHANNEL_NAME)
        # create_proxy keeps the Python callable alive across the FFI
        # boundary; without it the callback is collected and messages
        # silently stop arriving.
        self._on_message_proxy = create_proxy(self._on_message)
        self._channel.onmessage = self._on_message_proxy

        # Who this provider is, on a channel every tab on the origin
        # shares. One bridge serves the whole origin (see wallet_bridge.js),
        # and stamps every reply and every wallet event with the client it
        # belongs to, so a second tab's app never consumes ours.
        self._client = f"{js.Math.floor(js.Math.random() * 1e12):.0f}"
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, list[Callable[[Any], None]]] = {}
        self._counter = 0
        self._closed = False
        self.name = "Browser wallet"
        #: Populated by the handshake -- what the connector found.
        self.wallets: list[dict[str, Any]] = []

    # -- plumbing ----------------------------------------------------------

    def _to_js(self, value: Any):
        from pyodide.ffi import to_js  # noqa: PLC0415

        # dict_converter is required, and applies recursively, so nested tx
        # dicts arrive as real JS objects rather than Maps (which the wallet
        # would reject).
        return to_js(value, dict_converter=self._js.Object.fromEntries)

    def _on_message(self, event) -> None:
        try:
            message = event.data.to_py()
        except AttributeError:
            message = event.data
        if not isinstance(message, dict) or message.get("v") != PROTOCOL_VERSION:
            return

        # Addressed to another tab's app: not ours to act on.
        client = message.get("client")
        if client is not None and client != self._client:
            return

        direction = message.get("dir")
        if direction == "evt":
            for handler in self._handlers.get(message.get("event", ""), []):
                try:
                    handler(message.get("data"))
                except Exception:  # a bad handler must not kill the channel
                    pass
            return

        if direction != "res":
            return  # our own requests echo nowhere, but be defensive

        future = self._pending.pop(str(message.get("id")), None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error:
            future.set_exception(
                RpcError(
                    int(error.get("code", -32603)),
                    str(error.get("message", "Unknown wallet error")),
                    error.get("data"),
                )
            )
        else:
            future.set_result(message.get("result"))

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        self._counter += 1
        request_id = str(self._counter)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        self._channel.postMessage(
            self._to_js(
                {
                    "v": PROTOCOL_VERSION,
                    "dir": "req",
                    "id": request_id,
                    "client": self._client,
                    "method": method,
                    "params": params if params is not None else [],
                }
            )
        )

        if method == "bridge_selectWallet":
            timeout = CONNECT_TIMEOUT
        elif method in _INTERACTIVE:
            timeout = None
        else:
            timeout = REQUEST_TIMEOUT

        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            if method == "bridge_selectWallet":
                raise WalletUnavailable(
                    "The wallet did not finish connecting.\n\n"
                    "If you were scanning a WalletConnect QR code, close the "
                    "dialog and try again."
                ) from None
            raise WalletUnavailable(
                f"The wallet bridge did not answer '{method}' in time."
            ) from None

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def close(self) -> None:
        # Idempotent per the base-class contract: disconnect can arrive twice
        # (a click plus an empty `accountsChanged`), and destroying an
        # already-destroyed JsProxy raises.
        if self._closed:
            return
        self._closed = True
        try:
            self._channel.close()
        finally:
            self._on_message_proxy.destroy()

    # -- bridge-only methods -----------------------------------------------
    #
    # These never reach a wallet; assets/wallet_bridge.js answers them.

    async def handshake(self) -> dict[str, Any]:
        """Confirm the bridge is loaded and list the wallets it discovered."""
        self._counter += 1
        request_id = str(self._counter)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._channel.postMessage(
            self._to_js(
                {
                    "v": PROTOCOL_VERSION,
                    "dir": "req",
                    "id": request_id,
                    "client": self._client,
                    "method": "bridge_hello",
                    "params": [],
                }
            )
        )
        try:
            return await asyncio.wait_for(future, HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise WalletUnavailable(
                "The page loaded without the wallet bridge.\n\n"
                "assets/wallet_bridge.js must be included by index.html -- "
                "`flet publish` copies both out of src/assets/ for you, so "
                "this usually means the page is being served from a stale "
                "dist/ directory."
            ) from None

    async def configure(self, values: dict[str, Any]) -> None:
        """Hand local settings to the bridge before discovery.

        Config travels Python -> JS rather than living in a JS file so that
        a plain `flet publish` produces a configured build; see
        `wallet/settings.py`.
        """
        if values:
            await self.request("bridge_configure", [values])

    async def select_wallet(self, uuid: str) -> dict[str, Any]:
        """Choose which discovered wallet subsequent requests go to."""
        info = await self.request("bridge_selectWallet", [uuid])
        if isinstance(info, dict):
            self.name = info.get("name") or self.name
            # Which connector won changes what eth_sendTransaction means
            # (see WalletProvider.defers_execution), so it has to come back.
            self.connector = info.get("connector") or ""
        return info or {}


async def discover() -> BrowserWalletProvider:
    """Return a browser provider whose bridge answered, or raise."""
    if not is_browser():
        raise WalletUnavailable("Not running in a browser.")

    provider = BrowserWalletProvider()
    # Push local settings before asking what is available: whether the
    # WalletConnect connector can offer itself depends on the projectId.
    #
    # Printed, because "why is WalletConnect not in the list" has exactly
    # one answer -- the build has no projectId -- and it is otherwise
    # invisible: a missing `local_config.toml` is not an error, it is just
    # a connector that never appears.
    print(f"[wallet] {settings.describe()}")
    await provider.configure(settings.bridge_config())
    hello = await provider.handshake()
    provider.wallets = hello.get("wallets") or []
    if not provider.wallets:
        await provider.close()
        raise WalletUnavailable(
            "No EIP-1193 wallet found in this browser.\n\n"
            "Install MetaMask, Rabby, Frame's extension, or any wallet that "
            "announces itself via EIP-6963, then reload."
        )
    if len(provider.wallets) == 1:
        await provider.select_wallet(provider.wallets[0]["uuid"])
    return provider
