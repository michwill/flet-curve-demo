"""The API the app actually uses.

`base.WalletProvider` is the portability seam -- one method, EIP-1193, one
implementation per platform. This module is the layer above it: everything
a *token-sending app* needs, expressed in domain terms rather than RPC ones.

The split matters. A UI written against raw EIP-1193 ends up carrying the
whole wallet protocol -- discovery, account prompts, chain reads, ABI
encoding, the native-versus-ERC-20 branch -- as boilerplate. Everything in
this file is that boilerplate, written once:

    wallet = await Wallet.connect()
    for token in wallet.known_tokens():
        print(token.symbol, wallet.format(await wallet.balance_of(token), token))
    tx = await wallet.send(token=usdc, to="0x…", amount="12.5")

Nothing here imports Flet, and nothing here knows which platform it is on.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from . import chains, consent, erc20, icons
from .base import RpcError, WalletError, WalletProvider, WalletUnavailable
from .browser import is_browser
from .chains import Chain, Token


class InvalidRecipient(WalletError):
    """The destination address is missing, malformed, or fails EIP-55."""


class InvalidAmount(WalletError):
    """The amount is not a number, is out of range, or exceeds the balance."""


class InvalidToken(WalletError):
    """The address given for a custom token is not a readable ERC-20."""


class ConnectionCancelled(WalletError):
    """The user dismissed wallet selection."""

    rejected_by_user = True

    def __init__(self) -> None:
        super().__init__("Wallet selection cancelled.")


def _safe_icon(icon: object) -> str | None:
    """Accept `data:image/…` URIs and nothing else.

    Two reasons, and they happen to point the same way:

      * Security -- anything with a scheme or a host would let a wallet
        extension name a URL for the app to fetch merely by appearing in the
        picker, which leaks that the user opened it. EIP-6963 mandates a
        data URI precisely so a dapp never has to fetch anything.
      * Portability -- a *relative* path is not off-origin, but it renders
        on desktop and silently draws nothing on Flutter web (which resolves
        it against its own asset bundle, not the site root). Refusing it
        here means that trap cannot be re-introduced by accident; bundled
        icons go through `wallet.icons`, which returns data URIs.
    """
    if isinstance(icon, str) and icon.startswith("data:image/"):
        return icon
    return None


class WalletChoice:
    """One option offered when a platform found several wallets.

    `icon` is a `data:` URI ready to hand straight to an image widget --
    either announced by the wallet or bundled by this app -- or None, in
    which case draw a fallback from `initial`.
    """

    __slots__ = ("icon", "name", "rdns", "uuid")

    def __init__(self, uuid: str, name: str, rdns: str = "", icon: str | None = None):
        self.uuid = uuid
        self.name = name
        self.rdns = rdns
        self.icon = _safe_icon(icon)

    @property
    def initial(self) -> str:
        """Fallback avatar letter for a wallet that announced no icon."""
        return (self.name or "?").strip()[:1].upper() or "?"


#: Given the choices, return the uuid to use -- or None to cancel.
Chooser = Callable[[list[WalletChoice]], Awaitable[str | None]]


def autoconnect() -> bool:
    """Should the app connect at startup, without waiting for a click?

    Yes on desktop: the wallet is a local daemon the user already chose to
    run, there is nothing to select, and no popup is triggered by asking --
    so a "Connect wallet" button is pure ceremony. Either it is there and we
    use it, or it is not and the app should say so immediately.

    Unless the user disconnected. That is a decision, and reconnecting on
    the next launch would override it silently -- see `wallet.consent`.

    No in a browser: `eth_requestAccounts` raises a wallet popup, and doing
    that unprompted on page load is both hostile and likely to be blocked.
    """
    return not is_browser() and consent.autoconnect_allowed()


class Wallet:
    """A connected wallet: an account, on a chain, that can send tokens."""

    def __init__(
        self,
        provider: WalletProvider,
        address: str,
        chain: Chain,
        icon: str | None = None,
    ) -> None:
        self.provider = provider
        self.address = address
        self.chain = chain
        #: The chosen wallet's own icon as a `data:` URI, if it announced
        #: one. Kept from the choice rather than re-derived: only `connect`
        #: knows which of the announced wallets won.
        self.icon = _safe_icon(icon)
        self._change_handlers: list[Callable[[], Any]] = []
        self._disconnect_handlers: list[Callable[[], Any]] = []
        provider.on("accountsChanged", self._accounts_changed)
        provider.on("chainChanged", self._chain_changed)
        # An extension announces a revoked site by sending an empty
        # `accountsChanged`; WalletConnect closes the session and sends
        # `disconnect` instead. Both mean the same thing here.
        provider.on("disconnect", lambda _data: self._fire(self._disconnect_handlers))

    # -- lifecycle --------------------------------------------------------

    @classmethod
    async def connect(
        cls, choose: Chooser | None = None, *, always_choose: bool = False
    ) -> Wallet:
        """Find a wallet, authorise an account, and return a live session.

        `choose` is consulted when a platform offers more than one wallet --
        in practice, a browser with several connectors available. Omit it
        and the first is taken. Raises `WalletError` on every failure path,
        so callers need exactly one `except`.

        `always_choose` asks even when there is a single option. That is for
        "change wallet", where skipping the picker makes the command look
        broken: the app reconnects to the wallet you were already using and
        nothing on screen moves.
        """
        provider = await connect_provider()

        options = [
            WalletChoice(
                w["uuid"],
                w.get("name", "Wallet"),
                w.get("rdns", ""),
                # A wallet's own announced icon wins; otherwise fall back to
                # one this app bundles (WalletConnect announces none, being a
                # protocol rather than a wallet).
                w.get("icon") or icons.for_connector(w.get("connector")),
            )
            for w in getattr(provider, "wallets", [])
        ]
        uuid = options[0].uuid if options else ""
        if len(options) > 1 or (always_choose and options and choose):
            uuid = (await choose(options) if choose else options[0].uuid) or ""
            if not uuid:
                await provider.close()
                raise ConnectionCancelled()
            await provider.select_wallet(uuid)  # type: ignore[attr-defined]

        accounts = await provider.request_accounts()
        if not accounts:
            raise WalletUnavailable(
                "The wallet returned no accounts. Is it unlocked?"
            )

        # Which option won -- for its icon. With one wallet there was no
        # choice to make, but it still has a face.
        chosen = next(
            (o for o in options if o.uuid == uuid), options[0] if options else None
        )
        # Connecting answers the question a previous disconnect asked.
        consent.record_connect()
        chain = chains.get_chain(await provider.chain_id())
        return cls(
            provider,
            erc20.to_checksum_address(accounts[0]),
            chain,
            icon=chosen.icon if chosen else None,
        )

    async def close(self) -> None:
        """Let go of the transport, without calling it a disconnection.

        For swapping one live session for another: the old channel (or
        poller) has to be released, but nothing about the user's intent has
        changed, so neither the remembered wallet nor the consent marker is
        touched.
        """
        await self.provider.close()

    async def disconnect(self) -> None:
        """End the session because the user said so.

        Deliberate, so it is remembered: the page stops remembering this
        wallet and the marker covers the transports with no page storage --
        a desktop build otherwise connects again the moment it is
        relaunched.
        """
        consent.record_disconnect()
        forget = getattr(self.provider, "forget", None)
        if forget is not None:
            await forget()
        await self.close()

    @classmethod
    async def restore(cls) -> Wallet | None:
        """Reconnect to the wallet used last time, or return None.

        Closing a tab should not mean starting over, but reconnecting must
        not put a dialog in front of someone who only opened a page. So
        this asks `eth_accounts` -- what is *already* authorised -- and
        never `eth_requestAccounts`, which is the one that prompts. No
        authorisation, no session, and nothing is shown.

        The wallet is matched by rdns rather than uuid because an EIP-6963
        uuid is generated per page load, and by connector as a fallback so
        WalletConnect (whose entry is synthesised, not announced) is found
        the same way.
        """
        provider = await connect_provider()
        wanted = getattr(provider, "remembered", None)
        options = getattr(provider, "wallets", [])
        if not wanted or not options:
            await provider.close()
            return None

        def is_the_one(entry: dict[str, Any]) -> bool:
            # rdns is an identity; connector is only a category. Falling
            # back to the category when an rdns was stored would restore
            # "some injected wallet" -- in practice the first one in the
            # list, which is not the one that was connected.
            if wanted.get("rdns"):
                return bool(entry.get("rdns")) and entry["rdns"] == wanted["rdns"]
            return bool(wanted.get("connector")) and (
                entry.get("connector") == wanted["connector"]
            )

        match = next((w for w in options if is_the_one(w)), None)
        if match is None:
            # Uninstalled, or a different browser profile.
            await provider.close()
            return None

        try:
            await provider.select_wallet(  # type: ignore[attr-defined]
                match["uuid"], silent=True
            )
            accounts = await provider.accounts()
        except WalletError:
            await provider.close()
            return None
        if not accounts:
            # Still installed, but the site is no longer authorised (or the
            # wallet is locked). Not an error: just not connected.
            await provider.close()
            return None

        consent.record_connect()
        chain = chains.get_chain(await provider.chain_id())
        return cls(
            provider,
            erc20.to_checksum_address(accounts[0]),
            chain,
            icon=_safe_icon(
                match.get("icon") or icons.for_connector(match.get("connector"))
            ),
        )

    # -- identity ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable name of the wallet that answered."""
        return self.provider.name

    @property
    def short_address(self) -> str:
        return f"{self.address[:6]}…{self.address[-4:]}"

    @property
    def may_wait_on_cosigners(self) -> bool:
        """See `WalletProvider.may_wait_on_cosigners`."""
        return self.provider.may_wait_on_cosigners

    # -- events -----------------------------------------------------------

    def on_change(self, handler: Callable[[], Any]) -> None:
        """Called after the account or chain changed wallet-side."""
        self._change_handlers.append(handler)

    def on_disconnect(self, handler: Callable[[], Any]) -> None:
        """Called when the wallet drops the connection."""
        self._disconnect_handlers.append(handler)

    def _fire(self, handlers: list[Callable[[], Any]]) -> None:
        for handler in handlers:
            # A bad handler must not kill the event stream.
            with contextlib.suppress(Exception):
                handler()

    def _accounts_changed(self, accounts: Any) -> None:
        if isinstance(accounts, list) and accounts:
            self.address = erc20.to_checksum_address(accounts[0])
            self._fire(self._change_handlers)
        else:
            self._fire(self._disconnect_handlers)

    def _chain_changed(self, chain_id: Any) -> None:
        try:
            resolved = int(chain_id, 16) if isinstance(chain_id, str) else int(chain_id)
        except (TypeError, ValueError):
            return
        self.chain = chains.get_chain(resolved)
        self._fire(self._change_handlers)

    # -- tokens -----------------------------------------------------------

    def known_tokens(self) -> list[Token]:
        """The chain's native asset first, then its curated ERC-20s."""
        return [chains.native_token(self.chain), *self.chain.tokens]

    async def token_at(self, address: str) -> Token:
        """Read `symbol()`/`decimals()` off an arbitrary ERC-20.

        Goes through the wallet endpoint like everything else -- see the
        note in the README about WalletConnect being the one transport that
        does not proxy reads to the user's own node.
        """
        if not erc20.is_address(address):
            raise InvalidToken("That is not a valid contract address.")
        try:
            decimals = erc20.decode_uint(
                await self.provider.call(address, erc20.encode_decimals())
            )
            symbol = erc20.decode_string(
                await self.provider.call(address, erc20.encode_symbol())
            )
        except RpcError as exc:
            raise InvalidToken(f"Could not read that token: {exc.message}") from exc
        return Token(symbol or "TOKEN", erc20.to_checksum_address(address), decimals)

    async def balance_of(self, token: Token) -> int:
        """Balance in the token's smallest unit. Native and ERC-20 alike."""
        if token.is_native:
            return await self.provider.get_balance(self.address)
        return erc20.decode_uint(
            await self.provider.call(token.address, erc20.encode_balance_of(self.address))
        )

    @staticmethod
    def format(value: int, token: Token, precision: int = 6) -> str:
        return erc20.format_units(value, token.decimals, precision)

    @staticmethod
    def parse(amount: str, token: Token) -> int:
        try:
            return erc20.parse_units(amount, token.decimals)
        except ValueError as exc:
            raise InvalidAmount(str(exc)) from exc

    # -- sending ----------------------------------------------------------

    async def send(self, *, token: Token, to: str, amount: str, balance: int | None = None) -> str:
        """Validate, build and submit a transfer. Returns the transaction hash.

        Collapses the whole native-versus-ERC-20 distinction: same call
        either way. Validation raises `InvalidRecipient`/`InvalidAmount` so
        a UI can point at the offending field; pass `balance` to have the
        "more than you have" check done here too.
        """
        recipient = (to or "").strip()
        if not erc20.is_address(recipient):
            raise InvalidRecipient("Enter a valid 0x address")
        if erc20.has_checksum_case(recipient) and not erc20.is_checksum_address(recipient):
            # Mixed case that fails EIP-55 is a typo, not an old client.
            raise InvalidRecipient("Address checksum is invalid — check for typos")
        recipient = erc20.to_checksum_address(recipient)

        value = self.parse(amount, token)
        if value == 0:
            raise InvalidAmount("Amount must be greater than zero")
        if balance is not None and value > balance:
            raise InvalidAmount("More than your balance")

        # Gas and nonce are deliberately omitted: every wallet in scope
        # fills them in, and knows the chain better than this app does.
        if token.is_native:
            tx = {"from": self.address, "to": recipient, "value": hex(value)}
        else:
            tx = {
                "from": self.address,
                "to": token.address,
                "value": "0x0",
                "data": erc20.encode_transfer(recipient, value),
            }
        return await self.provider.send_transaction(tx)

    # -- links ------------------------------------------------------------

    def tx_url(self, tx_hash: str) -> str:
        return self.chain.tx_url(tx_hash)


async def connect_provider() -> WalletProvider:
    """Pick the transport for this platform. See `wallet/__init__.py`."""
    from . import connect_wallet

    return await connect_wallet()
