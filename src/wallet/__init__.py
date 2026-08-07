"""A wallet, on whatever platform this app happens to be running on.

Two layers, and an app should only ever need the second:

  `base.WalletProvider`  -- the portability seam. One method (`request`),
      EIP-1193, one implementation per platform:

          browser (Pyodide/wasm)  -> BroadcastChannel -> EIP-6963 / WalletConnect
          desktop (CPython)       -> HTTP JSON-RPC    -> Frame / qeth on :1248

  `session.Wallet`       -- what you actually call. Connection lifecycle,
      account and chain state, token metadata, balances, transfers.

The whole app-facing surface:

    from wallet import Wallet, WalletError, autoconnect

    wallet = await Wallet.connect()          # raises WalletError
    tokens = wallet.known_tokens()
    amount = await wallet.balance_of(tokens[0])
    tx     = await wallet.send(token=tokens[0], to="0x…", amount="0.25")
"""

from __future__ import annotations

from .base import RpcError, WalletError, WalletProvider, WalletUnavailable
from .browser import is_browser
from .chains import Chain, Token
from .session import (
    ConnectionCancelled,
    InvalidAmount,
    InvalidRecipient,
    InvalidToken,
    Wallet,
    WalletChoice,
    autoconnect,
)

__all__ = [
    # the app-facing API
    "Wallet",
    "WalletChoice",
    "autoconnect",
    "Token",
    "Chain",
    # errors -- all of them WalletError, so one `except` covers everything
    "WalletError",
    "WalletUnavailable",
    "RpcError",
    "InvalidRecipient",
    "InvalidAmount",
    "InvalidToken",
    "ConnectionCancelled",
    # the layer underneath, for anyone who needs raw EIP-1193
    "WalletProvider",
    "connect_wallet",
    "is_browser",
    "platform_name",
]


async def connect_wallet() -> WalletProvider:
    """Return a raw EIP-1193 provider for this platform.

    Most code wants `Wallet.connect()` instead; this is the seam underneath
    it, exposed for anything that needs to speak the protocol directly.
    Does not prompt for accounts -- that is `provider.request_accounts()`.
    """
    if is_browser():
        from . import browser

        return await browser.discover()

    from . import desktop

    return await desktop.discover()


def platform_name() -> str:
    return "browser" if is_browser() else "desktop"
