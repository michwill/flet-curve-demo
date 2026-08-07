"""The wallet panel, built rather than described.

This exists because the panel shipped broken: its action list referenced a
handler defined below it, so every click on the address raised instead of
opening anything. Nothing caught it -- the unit tests never built the
dialog and the browser check that turn only went as far as the picker.

Building it is enough to catch that class of mistake, and building it
needs no page: `_wallet_dialog` only touches `self.page` inside the
handlers it installs. So the app object is made without `__init__` (which
would want a real Flet page) and given the two attributes the method
actually reads.
"""

from __future__ import annotations

import flet as ft
import pytest

import main
from wallet import chains
from wallet.session import Wallet

ADDRESS = "0x6B175474E89094C44Da98b954EedeAC495271d0F"


class StubPage:
    #: `flet run --web` sets this while Python stays on the host, which is
    #: the case the panel explains.
    web = False

    def __init__(self) -> None:
        self.tasks: list[object] = []
        self.popped = 0

    def update(self) -> None:
        pass

    def pop_dialog(self) -> None:
        self.popped += 1

    def run_task(self, coro, *args) -> None:
        self.tasks.append(coro)


class StubProvider:
    name = "Mock Wallet"

    def on(self, event, handler) -> None:
        pass


def make_app() -> main.CurveApp:
    app = main.CurveApp.__new__(main.CurveApp)
    app.page = StubPage()
    app.wallet = make_wallet()
    return app


def make_wallet(icon: str | None = None) -> Wallet:
    return Wallet(StubProvider(), ADDRESS, chains.get_chain(1), icon=icon)


def buttons(dialog: ft.AlertDialog) -> list[str]:
    return [str(getattr(a, "content", "") or getattr(a, "text", "")) for a in dialog.actions]


def texts(control) -> list[str]:
    """Every string in a control tree, flattened."""
    found: list[str] = []
    if isinstance(control, ft.Text) and control.value:
        found.append(control.value)
    for child in (getattr(control, "controls", None) or []):
        found += texts(child)
    inner = getattr(control, "content", None)
    if isinstance(inner, ft.Control):
        found += texts(inner)
    return found


def test_the_panel_builds_at_all() -> None:
    """The regression: this raised NameError before anything was drawn."""
    dialog = make_app()._wallet_dialog(make_wallet())
    assert isinstance(dialog, ft.AlertDialog)


def test_it_shows_the_whole_address() -> None:
    """The reason the panel exists -- the header only has room for eleven
    characters of it."""
    dialog = make_app()._wallet_dialog(make_wallet())
    assert ADDRESS in texts(dialog.content)


def test_it_names_the_wallet_and_the_network() -> None:
    dialog = make_app()._wallet_dialog(make_wallet())
    assert "Mock Wallet" in texts(dialog.title)
    assert any("Ethereum" in line for line in texts(dialog.content))


def test_the_browser_can_change_wallet(monkeypatch) -> None:
    monkeypatch.setattr(main, "is_browser", lambda: True)
    dialog = make_app()._wallet_dialog(make_wallet())
    assert buttons(dialog) == ["Copy", "Change wallet", "Disconnect", "Close"]


def test_the_desktop_is_told_where_to_change_it_instead(monkeypatch) -> None:
    """One endpoint, no choice to offer -- but the wallet's own switcher
    now reaches the app, so say that rather than showing a dead button."""
    monkeypatch.setattr(main, "is_browser", lambda: False)
    dialog = make_app()._wallet_dialog(make_wallet())
    assert buttons(dialog) == ["Copy", "Disconnect", "Close"]
    assert any("switch account in the wallet" in t for t in texts(dialog.content))


@pytest.mark.parametrize("icon", [None, "data:image/svg+xml;base64,PHN2Zy8+"])
def test_every_button_has_a_handler(icon: str | None, monkeypatch) -> None:
    """A handler defined after the list it goes in is not a handler."""
    monkeypatch.setattr(main, "is_browser", lambda: True)
    dialog = make_app()._wallet_dialog(make_wallet(icon))
    assert all(callable(button.on_click) for button in dialog.actions)


def test_disconnect_closes_the_panel_and_runs_the_disconnect() -> None:
    app = make_app()
    dialog = app._wallet_dialog(make_wallet())
    disconnect = next(b for b in dialog.actions if b.content == "Disconnect")
    disconnect.on_click(None)
    assert app.page.popped == 1
    assert app.page.tasks == [app._disconnect_wallet]


def test_close_pops_without_touching_the_wallet() -> None:
    app = make_app()
    dialog = app._wallet_dialog(make_wallet())
    close = next(b for b in dialog.actions if b.content == "Close")
    close.on_click(None)
    assert app.page.popped == 1
    assert app.page.tasks == []


def test_an_announced_icon_is_drawn_and_a_missing_one_is_not() -> None:
    with_icon = main.wallet_mark("data:image/png;base64,iVBORw0KGgo=", "Rabby")
    without = main.wallet_mark(None, "Frame / qeth (127.0.0.1:1248)")
    assert isinstance(with_icon, ft.Image)
    # Not a lettered tile: "Frame / qeth" is two wallets and the letter
    # would name the wrong one.
    assert isinstance(without, ft.CircleAvatar)
    assert isinstance(without.content, ft.Icon)


def test_wallet_art_is_not_decoded_at_a_pixel_size() -> None:
    """It is usually SVG, which has none, and asking for one fails."""
    mark = main.wallet_mark("data:image/svg+xml;base64,PHN2Zy8+", "WalletConnect")
    assert mark.cache_width is None


# -- which transport is in play --------------------------------------------


def test_a_desktop_window_says_nothing_about_transports(monkeypatch) -> None:
    monkeypatch.setattr(main, "is_browser", lambda: False)
    app = make_app()
    app.page.web = False
    dialog = app._wallet_dialog(make_wallet())
    assert not any("Python is running" in t for t in texts(dialog.content))


def test_a_published_browser_build_says_nothing_either(monkeypatch) -> None:
    monkeypatch.setattr(main, "is_browser", lambda: True)
    app = make_app()
    app.page.web = True
    dialog = app._wallet_dialog(make_wallet())
    assert not any("Python is running" in t for t in texts(dialog.content))


def test_flet_run_web_explains_why_there_are_no_browser_wallets(monkeypatch) -> None:
    """Python on the host, client in a browser: the page looks published
    but the wallet layer is the local one, and nothing else is reachable."""
    monkeypatch.setattr(main, "is_browser", lambda: False)
    app = make_app()
    app.page.web = True
    dialog = app._wallet_dialog(make_wallet())
    note = [t for t in texts(dialog.content) if "Python is running" in t]
    assert note and "flet publish" in note[0]


# -- changing wallet, and changing your mind --------------------------------


class Recorder:
    """Stands in for a live `Wallet` so the app can be driven without one."""

    def __init__(self, address: str = ADDRESS) -> None:
        self.address = address
        self.short_address = f"{address[:6]}…{address[-4:]}"
        self.name = "qeth"
        self.icon = None
        self.chain = chains.get_chain(1)
        self.closed = False
        self.disconnected = False

    def on_change(self, handler) -> None:
        pass

    def on_disconnect(self, handler) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

    async def disconnect(self) -> None:
        self.disconnected = True
        self.closed = True


def app_with(wallet) -> main.CurveApp:
    """An app already connected, with just enough header to redraw."""
    app = main.CurveApp.__new__(main.CurveApp)
    app.page = StubPage()
    app.wallet = wallet
    app._detail = None
    app._page_name = "pools"      # not the portfolio, so nothing reloads
    app._address_expanded = False
    app.account_label = ft.Text("")
    app.account_chip = ft.Container(app.account_label)
    app.connect_button = ft.Button("Connect wallet")
    app.error = ft.Text("", visible=False)
    return app


async def test_cancelling_a_change_keeps_the_wallet_you_had(monkeypatch) -> None:
    """The bug: "change wallet" dropped the session before offering the
    picker, so backing out of the picker left you disconnected."""
    from wallet.session import ConnectionCancelled

    previous = Recorder()
    app = app_with(previous)

    async def refuse(**kwargs):
        raise ConnectionCancelled()

    monkeypatch.setattr(main.Wallet, "connect", refuse)
    await app._change_wallet()

    assert app.wallet is previous, "the old session was dropped"
    assert not previous.closed and not previous.disconnected
    assert not app.connect_button.visible, "still connected, so no Connect button"
    assert app.error.visible, "but say why nothing changed"


async def test_a_successful_change_releases_the_old_one(monkeypatch) -> None:
    """Released, not disconnected: nothing about intent has changed, so the
    remembered wallet and the consent marker must survive the swap."""
    previous, chosen = Recorder(), Recorder("0x" + "22" * 20)

    async def accept(**kwargs):
        return chosen

    monkeypatch.setattr(main.Wallet, "connect", accept)
    app = app_with(previous)
    await app._change_wallet()

    assert app.wallet is chosen
    assert previous.closed and not previous.disconnected


async def test_a_failed_first_connection_still_offers_the_button(
    monkeypatch,
) -> None:
    """Nothing to fall back to, so the Connect button has to come back."""
    from wallet.base import WalletUnavailable

    async def fail(**kwargs):
        raise WalletUnavailable("no wallet here")

    monkeypatch.setattr(main.Wallet, "connect", fail)
    app = app_with(None)
    await app.connect(None)

    assert app.wallet is None
    assert app.connect_button.visible
    assert "no wallet here" in app.error.value
