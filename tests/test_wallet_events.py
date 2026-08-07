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

from collections.abc import Callable
from typing import Any

import flet as ft
import pytest

from wallet.base import RpcError, WalletProvider, WalletUnavailable
from wallet.chains import get_chain
from wallet.desktop import DesktopWalletProvider
from wallet.session import Wallet

CHECKSUMMED = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
ACCOUNT = "0x1111111111111111111111111111111111111111"


class StubPage:
    """Enough of `ft.Page` for code that only redraws."""

    def update(self) -> None:
        pass

    def run_task(self, handler, *args):
        return None
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
    provider, _wallet, counts = make_wallet()
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


# -- picking up the previous session ---------------------------------------


class RestoringProvider(DiscoveringProvider):
    """Announces wallets and remembers one, as the browser bridge does."""

    def __init__(self, wallets, remembered, accounts=None) -> None:
        super().__init__(wallets)
        self.remembered = remembered
        self.accounts_answer = [CHECKSUMMED] if accounts is None else accounts
        self.asked: list[str] = []
        self.silently: list[str] = []
        self.forgotten = False

    async def select_wallet(self, uuid: str, *, silent: bool = False):
        self.selected = uuid
        if silent:
            self.silently.append(uuid)
        return {}

    async def forget(self) -> None:
        self.forgotten = True

    async def request(self, method: str, params=None):
        self.asked.append(method)
        if method == "eth_accounts":
            return self.accounts_answer
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_requestAccounts":
            raise AssertionError("restoring must never prompt")
        raise AssertionError(f"unexpected {method}")


async def restore_with(monkeypatch, provider):
    from wallet import session

    async def fake_connect_provider():
        return provider

    monkeypatch.setattr(session, "connect_provider", fake_connect_provider)
    return await Wallet.restore()


async def test_the_previous_wallet_comes_back(monkeypatch) -> None:
    provider = RestoringProvider(
        [{"uuid": "fresh-uuid", "name": "Rabby", "rdns": "io.rabby", "icon": PIXEL}],
        {"rdns": "io.rabby", "connector": "injected"},
    )
    wallet = await restore_with(monkeypatch, provider)
    assert wallet is not None
    assert wallet.address == CHECKSUMMED
    assert wallet.icon == PIXEL
    # Matched by rdns: the uuid is regenerated on every page load.
    assert provider.silently == ["fresh-uuid"]


async def test_restoring_never_prompts(monkeypatch) -> None:
    """`eth_accounts` reports what is already authorised; the other one asks."""
    provider = RestoringProvider(
        [{"uuid": "u", "name": "Rabby", "rdns": "io.rabby"}],
        {"rdns": "io.rabby", "connector": "injected"},
    )
    await restore_with(monkeypatch, provider)
    assert "eth_accounts" in provider.asked
    assert "eth_requestAccounts" not in provider.asked


async def test_a_locked_or_revoked_wallet_is_simply_not_connected(monkeypatch) -> None:
    provider = RestoringProvider(
        [{"uuid": "u", "name": "Rabby", "rdns": "io.rabby"}],
        {"rdns": "io.rabby", "connector": "injected"},
        accounts=[],
    )
    assert await restore_with(monkeypatch, provider) is None
    assert provider.closed


async def test_an_uninstalled_wallet_is_not_waited_for(monkeypatch) -> None:
    provider = RestoringProvider(
        [{"uuid": "u", "name": "Rabby", "rdns": "io.rabby"}],
        {"rdns": "com.gone", "connector": "injected"},
    )
    assert await restore_with(monkeypatch, provider) is None


async def test_walletconnect_is_matched_by_its_connector(monkeypatch) -> None:
    """Its entry is synthesised by the bridge, not announced by a wallet."""
    provider = RestoringProvider(
        [
            {"uuid": "walletconnect", "name": "WalletConnect", "rdns": "", "connector": "walletconnect"},
        ],
        {"rdns": "", "connector": "walletconnect"},
    )
    wallet = await restore_with(monkeypatch, provider)
    assert wallet is not None
    assert wallet.icon and wallet.icon.startswith("data:image/svg+xml;base64,")


async def test_nothing_remembered_means_nothing_happens(monkeypatch) -> None:
    provider = RestoringProvider(
        [{"uuid": "u", "name": "Rabby", "rdns": "io.rabby"}], None
    )
    assert await restore_with(monkeypatch, provider) is None
    assert provider.asked == []


async def test_disconnecting_stops_the_app_remembering(monkeypatch) -> None:
    """Otherwise the next page load would reconnect what you just left."""
    provider = RestoringProvider(
        [{"uuid": "u", "name": "Rabby", "rdns": "io.rabby"}],
        {"rdns": "io.rabby", "connector": "injected"},
    )
    wallet = await restore_with(monkeypatch, provider)
    assert wallet is not None
    await wallet.disconnect()
    assert provider.forgotten
    assert provider.closed


async def test_another_wallet_of_the_same_kind_is_not_restored(monkeypatch) -> None:
    """rdns is an identity; "injected" is a category.

    Falling back to the category when an rdns was stored restores whatever
    is first in the list -- which is how a remembered mock wallet came back
    as the qeth extension sitting above it.
    """
    provider = RestoringProvider(
        [
            {"uuid": "q", "name": "qeth", "rdns": "org.qeth", "connector": "injected"},
            {"uuid": "r", "name": "Rabby", "rdns": "io.rabby", "connector": "injected"},
        ],
        {"rdns": "io.rabby", "connector": "injected"},
    )
    wallet = await restore_with(monkeypatch, provider)
    assert wallet is not None
    assert provider.silently == ["r"], "restored the wrong wallet of the same kind"


async def test_a_remembered_wallet_that_is_gone_does_not_take_a_stand_in(
    monkeypatch,
) -> None:
    provider = RestoringProvider(
        [{"uuid": "q", "name": "qeth", "rdns": "org.qeth", "connector": "injected"}],
        {"rdns": "io.rabby", "connector": "injected"},
    )
    assert await restore_with(monkeypatch, provider) is None


# -- following the network picker ------------------------------------------
#
# Every read goes through the wallet's provider, so browsing one network
# with a wallet on another quotes addresses that hold no code there. The
# panels say so; picking a chain in the header also asks the wallet to
# come along, which is the fix rather than the warning.


class SwitchingProvider(WalletProvider):
    """Records switches, and can refuse or plead ignorance."""

    def __init__(self, chain: int = 1, *, knows: set[int] | None = None) -> None:
        self.chain = chain
        self.knows = {1} if knows is None else knows
        self.switched: list[int] = []
        self.added: list[dict] = []
        self.refuse = False

    async def request(self, method, params=None):
        params = params or []
        if method == "eth_chainId":
            return hex(self.chain)
        if method == "wallet_switchEthereumChain":
            wanted = int(params[0]["chainId"], 16)
            if self.refuse:
                raise RpcError(4001, "User rejected the request")
            if wanted not in self.knows:
                raise RpcError(4902, f"Unrecognized chain ID {hex(wanted)}")
            self.switched.append(wanted)
            self.chain = wanted
            return None
        if method == "wallet_addEthereumChain":
            self.added.append(params[0])
            self.knows.add(int(params[0]["chainId"], 16))
            self.chain = int(params[0]["chainId"], 16)
            return None
        raise AssertionError(f"unexpected {method}")


def curve_app(provider: SwitchingProvider, chain: str = "ethereum", chains=None):
    """`CurveApp` with its constructor skipped -- only the chain plumbing."""
    import main as app_module

    app = app_module.CurveApp.__new__(app_module.CurveApp)
    app.page = StubPage()
    app.chains = chains or {"ethereum": 1, "xdai": 100, "monad": 143}
    app.chain = chain
    app.wallet = Wallet(provider, ACCOUNT, get_chain(provider.chain))
    app.error = ft.Text("", visible=False)
    app.api = FakeLiteApi()
    return app


class FakeLiteApi:
    """Just the one call `align_wallet_chain` makes."""

    def __init__(self, chains=None) -> None:
        from curve.lite import LiteChain

        self.chains = chains if chains is not None else {
            "monad": LiteChain(
                "monad", 143, "Monad", 0.0,
                rpc_url="https://rpc.monad.xyz",
                explorer="https://explorer.monad.xyz",
                native_symbol="MON",
            )
        }

    async def lite_chains(self):
        return self.chains


async def test_picking_a_chain_takes_the_wallet_with_it() -> None:
    provider = SwitchingProvider(chain=1, knows={1, 100})
    app = curve_app(provider, chain="xdai")

    await app.align_wallet_chain()

    assert provider.switched == [100]


async def test_a_wallet_already_there_is_left_alone() -> None:
    """No prompt for a switch that would change nothing."""
    provider = SwitchingProvider(chain=100, knows={1, 100})
    app = curve_app(provider, chain="xdai")
    app.wallet = Wallet(provider, ACCOUNT, get_chain(100))

    await app.align_wallet_chain()

    assert provider.switched == []


async def test_a_refusal_is_reported_and_nothing_else_happens() -> None:
    """Refusing is a perfectly good answer; the panels' own notice stands."""
    provider = SwitchingProvider(chain=1, knows={1, 100})
    provider.refuse = True
    app = curve_app(provider, chain="xdai")

    await app.align_wallet_chain()

    assert provider.added == []
    assert app.error.visible is True
    assert "rejected" in app.error.value.lower()


async def test_an_unknown_lite_chain_is_offered_to_the_wallet() -> None:
    """4902 means the wallet has never heard of it, which for a Curve Lite
    chain is the normal case -- and their API is the only place publishing
    what EIP-3085 needs."""
    provider = SwitchingProvider(chain=1, knows={1})
    app = curve_app(provider, chain="monad")

    await app.align_wallet_chain()

    assert provider.added and provider.added[0]["chainId"] == hex(143)
    assert provider.added[0]["rpcUrls"] == ["https://rpc.monad.xyz"]
    assert provider.added[0]["nativeCurrency"]["symbol"] == "MON"


async def test_an_unknown_chain_with_no_metadata_says_so() -> None:
    """Gnosis is not a Lite chain, so nothing here can describe it to a
    wallet that does not know it. Saying so beats a silent no-op."""
    provider = SwitchingProvider(chain=1, knows={1})
    app = curve_app(provider, chain="xdai")
    app.api = FakeLiteApi(chains={})

    await app.align_wallet_chain()

    assert provider.added == []
    assert app.error.visible is True
    assert "does not know" in app.error.value


async def test_no_wallet_is_not_an_error() -> None:
    app = curve_app(SwitchingProvider(), chain="xdai")
    app.wallet = None
    await app.align_wallet_chain()  # must not raise


async def test_forgetting_survives_a_bridge_that_is_already_gone() -> None:
    """`forget` runs on an explicit disconnect, when the bridge may have
    stopped answering -- and the `except WalletError` that covers that used
    to name something the module had not imported, so the handler itself
    raised `NameError`. Found by ruff, which is why it is now pinned."""
    from wallet.browser import BrowserWalletProvider

    provider = BrowserWalletProvider.__new__(BrowserWalletProvider)

    async def refuse(method, params=None):
        raise WalletUnavailable("the bridge is gone")

    provider.request = refuse  # type: ignore[method-assign]
    await provider.forget()   # must not raise
