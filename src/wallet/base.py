"""The seam that makes this app cross-platform: EIP-1193.

Every wallet this app can reach -- a browser extension behind a JS
connector, or a desktop wallet like Frame/qeth listening on localhost --
speaks the same request/response protocol:

    await provider.request("eth_sendTransaction", [tx]) -> "0x<hash>"

So the *only* thing that differs per platform is the transport. Pick the
transport once at startup (see `wallet/__init__.py`) and every layer above
this file -- ERC-20 encoding, balance reads, the whole Flet UI -- is
written once and runs everywhere.

Note that a wallet endpoint is not just a signer: unknown methods are
proxied to the chain's node (Frame and qeth both do this, and a browser
extension does too), so `eth_call`/`eth_getBalance` ride the same channel.
That is why this app needs no RPC URL of its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

# EIP-1193 standard error codes we actually care about.
USER_REJECTED_REQUEST = 4001
UNAUTHORIZED = 4100
UNSUPPORTED_METHOD = 4200
DISCONNECTED = 4900
CHAIN_DISCONNECTED = 4901
UNRECOGNIZED_CHAIN = 4902


class WalletError(Exception):
    """Base for everything this package raises.

    Exists so an app can get away with a single `except WalletError` and
    show `str(exc)`: every subclass renders itself as a sentence fit for a
    user, not a stack trace.
    """

    #: Did the human say no, or did something break? Almost everything
    #: here is the second, and the UI paints it red and leaves it up.
    #:
    #: A refusal is neither. It is the answer to a question the app asked,
    #: and the person who gave it already knows what they clicked -- so
    #: telling them "Rejected in the wallet" in red reports a failure that
    #: did not happen. The exceptions that *are* a refusal say so here,
    #: and the UI clears the line instead.
    rejected_by_user = False


def quantity(value: Any, what: str) -> int:
    """A number from a wallet, whatever shape it arrived in.

    JSON-RPC calls these QUANTITY and says they are hex strings, and most
    wallets send hex strings. Not all of them: a provider answering from
    its own state -- WalletConnect's `eth_chainId` is the one that bit --
    hands back a plain JavaScript number, which arrives here as an `int`.
    `int(1, 16)` then raises "int() can't convert non-string with explicit
    base", an error that names neither the wallet nor the call and reads
    like a bug in the parsing rather than a wallet being loose with the
    spec. It cost an afternoon.

    So the shape is accepted and the *absence* of an answer is what gets a
    sentence. `wallet/session.py` and `curve/confirm.py` each grew their
    own copy of the first half of this; this is the one they should have
    shared.
    """
    if isinstance(value, str):
        return int(value, 16) if value[:2].lower() == "0x" else int(value)
    # `bool` is an `int` in Python and never a quantity in JSON-RPC.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    raise WalletError(f"The wallet did not say what {what} is.")


class RpcError(WalletError):
    """An error the wallet (or the node behind it) returned."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:
        return "Rejected in the wallet." if self.rejected_by_user else self.message

    @property
    def rejected_by_user(self) -> bool:
        """True when the human clicked "reject" rather than something breaking.

        4001 is the EIP-1193 code every browser wallet uses. qeth reports a
        cancelled signing dialog as -32000 with a message, so match that
        text too -- otherwise a plain cancel looks like a crash in the UI.
        """
        if self.code == USER_REJECTED_REQUEST:
            return True
        return self.code == -32000 and any(
            word in self.message.lower() for word in ("cancel", "reject", "denied")
        )


class WalletUnavailable(WalletError):
    """No wallet transport could be reached on this platform.

    Carries a human-readable hint about how to fix it, because the fix is
    very different in a browser (install MetaMask) versus on desktop (start
    Frame or qeth).
    """


class WalletProvider(ABC):
    """An EIP-1193 provider. Subclasses implement exactly one method."""

    #: Human-readable transport name, shown in the UI.
    name: str = "wallet"
    #: "browser" or "desktop" -- used only for wording in the UI.
    kind: str = "unknown"
    #: Which connector answered, when the transport has several
    #: ("injected", "walletconnect"). Empty when it has only one.
    connector: str = ""

    @property
    def may_wait_on_cosigners(self) -> bool:
        """True when a send can legitimately stay pending for a very long time.

        Over WalletConnect the wallet on the other end may be a Safe (or any
        multisig). Safe holds the `eth_sendTransaction` response open until
        enough owners have signed *and* the transaction has executed, then
        returns the real on-chain hash. For a 2-of-3 Safe that can be hours.

        Two consequences, both of which this app depends on:
          - the request must have no timeout (see `browser._INTERACTIVE`),
            or a perfectly good multisig send would be failed spuriously;
          - the UI must say so, or an honest wait looks like a hang.
        """
        return self.connector == "walletconnect"

    @abstractmethod
    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        """Send an EIP-1193 request. Raise `RpcError` on a wallet/node error."""

    async def close(self) -> None:
        """Release transport resources. Safe to call more than once."""

    # -- event subscription ------------------------------------------------
    #
    # Browser wallets push `accountsChanged`/`chainChanged`; qeth pushes the
    # same over its WebSocket. The default implementation is a no-op so a
    # transport that cannot deliver events still satisfies the interface and
    # the UI just relies on its own refreshes.

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        """Subscribe to a wallet event (`accountsChanged`, `chainChanged`)."""

    # -- conveniences ------------------------------------------------------
    #
    # Everything below is written once, in terms of `request`, and is
    # therefore shared by every platform.

    async def request_accounts(self) -> list[str]:
        """Prompt for connection. Returns the authorised accounts."""
        return await self.request("eth_requestAccounts") or []

    async def accounts(self) -> list[str]:
        """Already-authorised accounts. Does not prompt."""
        return await self.request("eth_accounts") or []

    async def chain_id(self) -> int:
        return quantity(await self.request("eth_chainId"), "the network")

    async def get_balance(self, address: str) -> int:
        """Native-token balance in wei."""
        return quantity(
            await self.request("eth_getBalance", [address, "latest"]), "a balance"
        )

    async def call(self, to: str, data: str) -> str:
        """`eth_call` against the latest block. Returns hex-encoded return data."""
        return await self.request("eth_call", [{"to": to, "data": data}, "latest"])

    async def block_number(self) -> int:
        """The chain head, as this endpoint currently sees it."""
        return quantity(await self.request("eth_blockNumber"), "the block number")

    async def transaction_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """The receipt, or None while the transaction is still pending."""
        return await self.request("eth_getTransactionReceipt", [tx_hash])

    async def send_transaction(self, tx: dict[str, Any]) -> str:
        """Ask the wallet to sign+broadcast. Returns the transaction hash.

        Deliberately does not set gas or nonce: every wallet in scope fills
        those in itself, and guessing them here would only create a way for
        this app to be wrong on a chain it does not know about.
        """
        return await self.request("eth_sendTransaction", [tx])

    async def switch_chain(self, chain_id: int) -> None:
        await self.request("wallet_switchEthereumChain", [{"chainId": hex(chain_id)}])

    async def add_chain(self, params: dict[str, Any]) -> None:
        """Teach the wallet a network it does not know (EIP-3085).

        Only worth calling after `switch_chain` has answered 4902, which
        is how a wallet says "never heard of it". The parameters are the
        wallet's to display and the user's to accept or refuse; nothing
        here can add a network on its own.
        """
        await self.request("wallet_addEthereumChain", [params])
