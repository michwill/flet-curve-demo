"""What the Swap tab says when a quoted route cannot be sent, and where it
puts the route once it has one.

A route that quotes is not always a route that can be *shipped*: the router
refuses to encode one whose legs are too small to carry a minimum rate, which
is it declining to send something it cannot protect rather than anything
having gone wrong.  The quote itself is still the chain's own number.
"""

from __future__ import annotations

from ui.responsive import layout_for
from ui.swap import SwapView
from ui.swap_page import _why_unsendable


class EncodingError(RuntimeError):
    """Named as the router names it, since the mapping keys off that."""


def test_a_leg_too_small_to_protect_is_said_in_one_line():
    """The router's own message is written for a terminal.

    Verbatim from a real refusal -- it names both pools, the wei in and out of
    each, and the flag that would ship it unprotected.  Right for someone
    debugging a route, and far too much under a swap button.
    """
    exc = EncodingError(
        "leg(s) 3 on 0xf05Bc5C38a8F5E3D98c72f11bC59E713F8a32228 (9329968064 "
        "in, 0 out), 5 on 0x8273Cb2cF9AF3228fD14AF25B5B1De2A9676C372 "
        "(35540624523651 in, 35 out) produce too little for a minimum rate to "
        "bound -- one unit of the output is more than the tolerance.  A leg "
        "worth that little is not worth executing; re-solve without it, or "
        "pass allow_unbounded to ship it unprotected"
    )
    said = _why_unsendable(exc)
    assert said == "This route has a leg too small to protect, so it cannot be sent"
    assert "0x" not in said and "allow_unbounded" not in said


def test_another_encoding_refusal_still_says_something_useful():
    assert _why_unsendable(EncodingError("32 legs is the most")) == (
        "This route cannot be packed into one call"
    )


def test_anything_else_is_reported_rather_than_hidden():
    said = _why_unsendable(RuntimeError("the endpoint went away\nand the rest"))
    assert said.startswith("This route cannot be sent: the endpoint went away")
    assert "\n" not in said, "one line, whatever the exception did"


def build_view():
    """A `SwapView` with nothing behind it.

    It only ever reads `page` for theme colours at draw time, so a bare object
    is enough to exercise its layout arithmetic without a window.
    """
    class NoPage:
        pass

    return SwapView(NoPage(), "ethereum", on_amount=lambda *_: None,
                    on_pair=lambda *_: None, on_max=lambda *_: None,
                    on_approve=lambda *_: None, on_swap=lambda *_: None)


def test_the_first_route_does_not_disturb_what_is_being_typed():
    """The stacked route appears without rebuilding the widget above it.

    It used to be added to the column when the first quote arrived, and that
    is an update of the subtree the amount field lives in -- which sends the
    server's copy of the field back to the browser, over whatever has been
    typed since.  Someone typing "2000000" watched it become "2" at the
    moment their first answer appeared.
    """
    view = build_view()
    view.set_layout(layout_for(400))
    assert view._stacked
    view.amount.value = "2000000"
    before = view._body.controls[0]

    view.show_route(object())

    assert view._body.controls[0] is before, "the same column, untouched"
    assert view.amount.value == "2000000"
    assert view.diagram.visible


def test_a_route_that_goes_away_hides_the_frame_again():
    view = build_view()
    view.set_layout(layout_for(400))
    view.show_route(object())
    view.show_route(None)
    assert not view.diagram.visible


def test_a_wide_window_keeps_the_frame_beside_the_widget_throughout():
    """Route or no route: the frame appearing on the first quote used to
    shift the widget sideways just as someone finished typing into it."""
    view = build_view()
    view.set_layout(layout_for(1400))
    assert not view._stacked
    assert view.diagram in view._body.controls and view.diagram.visible
    view.show_route(object())
    assert view.diagram in view._body.controls and view.diagram.visible


def test_a_failure_says_which_block_it_happened_at():
    """A router that will not price a pair it priced a minute ago is a thing
    to reproduce, and reproducing it means pinning the state."""
    from ui.swap_page import _with_block

    exc = RuntimeError("src not connected to dst through the active set")
    assert _with_block(exc, 25_812_795) == (
        "src not connected to dst through the active set (block 25,812,795)"
    )


def test_a_failure_with_no_block_to_name_says_the_rest_anyway():
    from ui.swap_page import _with_block

    assert _with_block(RuntimeError("the endpoint went away"), 0) == (
        "the endpoint went away"
    )
    assert _with_block(RuntimeError(""), 0) == "RuntimeError"


class Wallet:
    """A wallet that arrives after the tab is already open."""

    def __init__(self) -> None:
        self.address = ""
        self.balances: dict[str, int] = {}

    def connect(self, address: str, **balances: int) -> None:
        self.address = address
        self.balances = {token.lower(): held for token, held in balances.items()}


class Reader:
    """Answers `balanceOf` from the wallet, and nothing else."""

    def __init__(self, wallet: Wallet) -> None:
        self.wallet = wallet

    async def call(self, to: str, data: str) -> str:
        held = self.wallet.balances.get(to.lower(), 0)
        return "0x" + f"{held:064x}"

    async def get_balance(self, _address: str) -> int:
        return 0


def swap_page_with(wallet: Wallet, coins):
    """A `SwapPage` wired to that wallet and nothing else that needs a network."""
    from ui.swap_page import SwapPage

    class NoPage:
        def run_task(self, *_a, **_kw) -> None:
            pass

    page = SwapPage(
        NoPage(),
        api=None,
        chain_name=lambda: "ethereum",
        chain_id=lambda: 1,
        provider_for=lambda: Reader(wallet),
        account=lambda: wallet.address,
        on_loading=lambda *_a: None,
        on_loaded=lambda *_a: None,
    )
    page.chain_id_now = 1
    page.view.offer(coins, "ethereum")
    page.view.set_pair(coins[0], coins[1])
    return page


def two_coins():
    from router.universe import CoinEntry

    return [
        CoinEntry("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC",
                  "USD Coin", 6, 0.0, 0),
        CoinEntry("0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT",
                  "Tether", 6, 0.0, 0),
    ]


async def test_a_wallet_that_connects_after_the_tab_is_open_still_shows_a_balance():
    """Everything wallet-shaped here is read through a callable, so nothing
    was stale -- it was simply never asked again."""
    coins = two_coins()
    wallet = Wallet()
    page = swap_page_with(wallet, coins)

    await page._read_balances()
    assert page.view.amount.hint_text == "0.0", "nobody connected, nothing to show"

    wallet.connect("0x" + "11" * 20, **{coins[0].address: 8_598_432_131})
    await page.wallet_changed()

    assert page.view.amount.hint_text == "8,598.43"


async def test_max_works_once_the_wallet_is_there():
    coins = two_coins()
    wallet = Wallet()
    page = swap_page_with(wallet, coins)
    page._max_clicked()
    assert page.view.amount.value in ("", None), "no balance, nothing to fill"

    wallet.connect("0x" + "11" * 20, **{coins[0].address: 2_500_000})
    await page.wallet_changed()
    page._max_clicked()

    assert page.view.amount.value == "2.5"


async def test_a_wallet_going_away_takes_the_balance_with_it():
    """A figure left on screen after the wallet has gone is a figure for
    nobody."""
    coins = two_coins()
    wallet = Wallet()
    page = swap_page_with(wallet, coins)
    wallet.connect("0x" + "11" * 20, **{coins[0].address: 8_598_432_131})
    await page.wallet_changed()
    assert page.view.amount.hint_text == "8,598.43"

    wallet.address = ""
    await page.wallet_changed()

    assert page.view.amount.hint_text == "0.0"
