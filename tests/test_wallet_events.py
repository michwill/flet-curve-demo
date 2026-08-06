"""What a `Wallet` does when the wallet itself changes something.

These are the events nobody clicks: the user picks another account in
MetaMask, switches network, or revokes the site, and the page has to
notice. There is no request/response to hang the behaviour off -- the
provider simply pushes -- so the whole contract is "did the session update
and did the app get told".

Nothing here touches a browser: `Wallet` subscribes through
`WalletProvider.on`, which the fake below implements by keeping the
handlers in a dict and calling them on demand.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from wallet.base import WalletProvider, WalletUnavailable
from wallet.desktop import DesktopWalletProvider
from wallet.session import Wallet

CHECKSUMMED = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
OTHER = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


class FakeProvider(WalletProvider):
    """Records subscriptions and lets a test push events through them."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[[Any], None]]] = {}
        self.closed = False

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, data: Any) -> None:
        for handler in list(self.handlers.get(event, [])):
            handler(data)

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        raise AssertionError("these tests send no requests")

    async def close(self) -> None:
        self.closed = True


def make_wallet() -> tuple[FakeProvider, Wallet, dict[str, int]]:
    provider = FakeProvider()
    from wallet import chains

    wallet = Wallet(provider, CHECKSUMMED, chains.get_chain(1))
    counts = {"change": 0, "gone": 0}
    wallet.on_change(lambda: counts.__setitem__("change", counts["change"] + 1))
    wallet.on_disconnect(lambda: counts.__setitem__("gone", counts["gone"] + 1))
    return provider, wallet, counts


def test_a_new_account_replaces_the_old_one() -> None:
    provider, wallet, counts = make_wallet()
    provider.emit("accountsChanged", [OTHER])
    assert wallet.address == OTHER
    assert counts["change"] == 1


def test_a_lowercase_account_is_checksummed_on_the_way_in() -> None:
    """Wallets are inconsistent about case; the UI should not be."""
    provider, wallet, _ = make_wallet()
    provider.emit("accountsChanged", [OTHER.lower()])
    assert wallet.address == OTHER


def test_an_empty_account_list_is_a_disconnection() -> None:
    """How an extension says the site was revoked."""
    provider, wallet, counts = make_wallet()
    provider.emit("accountsChanged", [])
    assert counts == {"change": 0, "gone": 1}


def test_a_disconnect_event_is_also_a_disconnection() -> None:
    """How WalletConnect says the same thing."""
    provider, _wallet, counts = make_wallet()
    provider.emit("disconnect", {"code": 4900, "message": "Session closed"})
    assert counts["gone"] == 1


@pytest.mark.parametrize("payload", ["0xa4b1", 42161])
def test_the_chain_follows_either_spelling(payload: object) -> None:
    """`chainChanged` carries hex from a browser and an int from qeth."""
    provider, wallet, counts = make_wallet()
    provider.emit("chainChanged", payload)
    assert wallet.chain.chain_id == 42161
    assert counts["change"] == 1


def test_an_unparseable_chain_is_ignored() -> None:
    """Better a stale chain than a crash inside a provider callback."""
    provider, wallet, counts = make_wallet()
    provider.emit("chainChanged", "not-a-chain")
    assert wallet.chain.chain_id == 1
    assert counts["change"] == 0


def test_an_unknown_chain_still_produces_a_usable_one() -> None:
    provider, wallet, _ = make_wallet()
    provider.emit("chainChanged", "0x2329")  # 9001, not in the table
    assert wallet.chain.chain_id == 9001
    assert wallet.chain.name  # a placeholder, but never empty


def test_one_bad_handler_does_not_stop_the_others() -> None:
    """A UI callback that raises must not break the event stream.

    The handlers are Flet callbacks; one of them touching an unmounted
    control would otherwise swallow every later account change.
    """
    provider, wallet, _ = make_wallet()
    seen: list[str] = []
    wallet.on_change(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    wallet.on_change(lambda: seen.append("after"))
    provider.emit("accountsChanged", [OTHER])
    assert seen == ["after"]


def test_the_short_form_follows_the_new_account() -> None:
    """What the header actually draws."""
    provider, wallet, _ = make_wallet()
    provider.emit("accountsChanged", [OTHER])
    assert wallet.short_address == f"{OTHER[:6]}…{OTHER[-4:]}"


# -- which wallet answered -------------------------------------------------


class DiscoveringProvider(FakeProvider):
    """A provider that announces wallets, the way the browser bridge does."""

    def __init__(self, wallets: list[dict[str, Any]]) -> None:
        super().__init__()
        self.wallets = wallets
        self.selected = ""

    async def select_wallet(self, uuid: str) -> dict[str, Any]:
        self.selected = uuid
        return {}

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        if method == "eth_requestAccounts":
            return [CHECKSUMMED]
        if method == "eth_chainId":
            return "0x1"
        raise AssertionError(f"unexpected {method}")


async def connect_with(monkeypatch, wallets, choose=None) -> Wallet:
    from wallet import session

    provider = DiscoveringProvider(wallets)

    async def fake_connect_provider() -> WalletProvider:
        return provider

    monkeypatch.setattr(session, "connect_provider", fake_connect_provider)
    return await Wallet.connect(choose=choose)


PIXEL = "data:image/png;base64,iVBORw0KGgo="


async def test_the_wallet_keeps_the_icon_of_the_one_that_was_chosen(monkeypatch) -> None:
    """The panel draws it, so the choice has to outlive the picker."""

    async def choose(options):
        return options[1].uuid

    wallet = await connect_with(
        monkeypatch,
        [
            {"uuid": "a", "name": "Rabby", "icon": PIXEL},
            {"uuid": "b", "name": "MetaMask", "icon": PIXEL.replace("iVBO", "iVBP")},
        ],
        choose=choose,
    )
    assert wallet.name  # provider name, untouched by this
    assert wallet.icon == PIXEL.replace("iVBO", "iVBP")


async def test_a_single_wallet_still_has_a_face(monkeypatch) -> None:
    """No picker appears, but the panel still needs the icon."""
    wallet = await connect_with(
        monkeypatch, [{"uuid": "only", "name": "Rabby", "icon": PIXEL}]
    )
    assert wallet.icon == PIXEL


async def test_walletconnect_falls_back_to_the_bundled_icon(monkeypatch) -> None:
    """It announces none, being a protocol rather than a wallet."""
    wallet = await connect_with(
        monkeypatch,
        [{"uuid": "wc", "name": "WalletConnect", "connector": "walletconnect"}],
    )
    assert wallet.icon and wallet.icon.startswith("data:image/svg+xml;base64,")


async def test_a_wallet_with_no_icon_at_all_has_none(monkeypatch) -> None:
    """The UI draws a lettered tile; None is the signal for it."""
    wallet = await connect_with(monkeypatch, [{"uuid": "x", "name": "Nameless"}])
    assert wallet.icon is None


async def test_a_remote_icon_url_is_refused(monkeypatch) -> None:
    """A relative or http src would be resolved against the asset bundle.

    `_safe_icon` already guards the picker; the same guard has to hold for
    the icon the session carries around.
    """
    wallet = await connect_with(
        monkeypatch,
        [{"uuid": "x", "name": "Sketchy", "icon": "https://example.com/icon.png"}],
    )
    assert wallet.icon is None


async def test_a_desktop_provider_announces_nothing_and_still_connects(
    monkeypatch,
) -> None:
    """qeth has no wallet list; `connect` must not index into an empty one."""
    wallet = await connect_with(monkeypatch, [])
    assert wallet.address == CHECKSUMMED
    assert wallet.icon is None


# -- the desktop poller ----------------------------------------------------
#
# An HTTP endpoint cannot push, so `DesktopWalletProvider` synthesises the
# same events by asking. These drive it through one pass at a time rather
# than waiting on the real interval.


class ScriptedDesktop(DesktopWalletProvider):
    """A desktop provider whose RPC answers come from a list."""

    def __init__(self, answers: list[tuple[list[str], str]]) -> None:
        super().__init__("http://127.0.0.1:1248")
        self.answers = answers
        self.events: list[tuple[str, Any]] = []
        self.on("accountsChanged", lambda d: self.events.append(("accounts", d)))
        self.on("chainChanged", lambda d: self.events.append(("chain", d)))

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        if not self.answers:
            self._closed = True  # end the loop the way close() would
            raise WalletUnavailable("no more answers")
        accounts, chain = self.answers[0]
        if method == "eth_accounts":
            return accounts
        if method == "eth_chainId":
            self.answers.pop(0)
            return chain
        raise AssertionError(f"unexpected {method}")


async def run_poller(provider: DesktopWalletProvider, monkeypatch) -> None:
    """Run the poll loop with the wait taken out."""
    import wallet.desktop as desktop

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(desktop.asyncio, "sleep", no_wait)
    await provider._poll()


async def test_the_first_pass_only_seeds(monkeypatch) -> None:
    """Connecting is not an account change."""
    provider = ScriptedDesktop([([CHECKSUMMED], "0x1")])
    await run_poller(provider, monkeypatch)
    assert provider.events == []


async def test_switching_account_in_the_wallet_is_reported(monkeypatch) -> None:
    """The whole point: nothing in the app clicked anything."""
    provider = ScriptedDesktop([([CHECKSUMMED], "0x1"), ([OTHER], "0x1")])
    await run_poller(provider, monkeypatch)
    assert provider.events == [("accounts", [OTHER])]


async def test_switching_network_in_the_wallet_is_reported(monkeypatch) -> None:
    provider = ScriptedDesktop([([CHECKSUMMED], "0x1"), ([CHECKSUMMED], "0xa4b1")])
    await run_poller(provider, monkeypatch)
    assert provider.events == [("chain", "0xa4b1")]


async def test_locking_the_wallet_reports_no_accounts(monkeypatch) -> None:
    """Which `Wallet` reads as a disconnection, exactly as in a browser."""
    provider = ScriptedDesktop([([CHECKSUMMED], "0x1"), ([], "0x1")])
    await run_poller(provider, monkeypatch)
    assert provider.events == [("accounts", [])]


async def test_a_steady_wallet_says_nothing(monkeypatch) -> None:
    same = [([CHECKSUMMED], "0x1")] * 4
    provider = ScriptedDesktop(same)
    await run_poller(provider, monkeypatch)
    assert provider.events == []


async def test_a_wallet_that_stops_answering_is_not_a_disconnection(
    monkeypatch,
) -> None:
    """A locked or restarting wallet must not drop the session.

    `request` raising is the transient case; only an empty account list
    means the site was actually revoked.
    """
    provider = ScriptedDesktop([([CHECKSUMMED], "0x1")])
    await run_poller(provider, monkeypatch)  # runs dry, then raises
    assert provider.events == []


async def test_closing_stops_the_poller() -> None:
    provider = DesktopWalletProvider("http://127.0.0.1:1248")
    provider.on("accountsChanged", lambda _d: None)
    assert provider._watch is not None
    await provider.close()
    assert provider._watch is None
