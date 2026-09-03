"""What the Swap tab says when a quoted route cannot be sent, and where it
puts the route once it has one.

A route that quotes is not always a route that can be *shipped*: the router
refuses to encode one whose legs are too small to carry a minimum rate, which
is it declining to send something it cannot protect rather than anything
having gone wrong.  The quote itself is still the chain's own number.
"""

from __future__ import annotations

from router import Stage
from ui import swap
from ui.responsive import layout_for
from ui.status import FAILED
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


def view_with(coins):
    """Just the `SwapView`, with no network behind it."""
    from ui.swap import SwapView

    class NoPage:
        def run_task(self, *_a, **_kw) -> None:
            pass

    view = SwapView(NoPage(), "ethereum", on_amount=lambda *_: None,
                    on_pair=lambda *_: None, on_max=lambda *_: None,
                    on_approve=lambda *_: None, on_swap=lambda *_: None)
    view.offer(coins, "ethereum")
    return view


def held_and_not():
    """Two coins in volume order, the second of which the wallet holds."""
    from router.universe import CoinEntry

    return [
        CoinEntry("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC",
                  "USD Coin", 6, volume=9.0),
        CoinEntry("0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT",
                  "Tether", 6, volume=1.0),
    ]


def test_only_the_selling_side_is_reordered_by_what_is_held():
    """What somebody already holds says nothing about what they want to buy."""
    from router import holdings

    coins = held_and_not()
    ranked = holdings.rank(
        coins, {coins[1].address: 5_000_000}, {coins[1].address: 1.0})
    view = view_with(coins)

    view.offer(coins, "ethereum", owned=ranked)

    assert [c.symbol for c in view.sell._entries] == ["USDT", "USDC"]
    assert [c.symbol for c in view.buy._entries] == ["USDC", "USDT"]


def test_the_buying_side_shows_no_balance_against_a_coin():
    """A balance beside a coin someone is buying answers a question they did
    not ask."""
    from router import holdings

    coins = held_and_not()
    ranked = holdings.rank(
        coins, {coins[1].address: 5_000_000}, {coins[1].address: 1.0})
    view = view_with(coins)

    view.offer(coins, "ethereum", owned=ranked)

    assert view.sell._entries[0].balance == 5_000_000
    assert all(entry.balance == 0 for entry in view.buy._entries)


def test_with_nothing_held_both_sides_are_the_same_list():
    coins = held_and_not()
    view = view_with(coins)

    view.offer(coins, "ethereum", owned=None)

    assert [c.symbol for c in view.sell._entries] == ["USDC", "USDT"]
    assert [c.symbol for c in view.buy._entries] == ["USDC", "USDT"]


class Typed:
    """A `SearchBar` event, which carries what is in the box as `data`."""

    def __init__(self, data: str = "") -> None:
        self.data = data


def many_coins(count: int):
    from router.universe import CoinEntry

    return [
        CoinEntry(f"0x{n:040x}", f"C{n}", f"Coin {n}", 18, 0.0, 0)
        for n in range(count)
    ]


def test_the_picker_draws_a_screenful_rather_than_the_whole_chain():
    """Ethereum offers 301 coins and a tile apiece carries a logo, which is
    301 controls to open the list and 155 more on the "u" of "usdc"."""
    from ui.swap import ROWS_SHOWN

    coins = many_coins(301)
    view = view_with(coins)

    rows = view.sell._rows()

    assert len(rows) == ROWS_SHOWN + 1, "a screenful, and a line about the rest"
    assert "261 more" in rows[-1].content.value


def test_a_list_that_fits_says_nothing_about_more():
    coins = many_coins(5)
    view = view_with(coins)
    assert len(view.sell._rows()) == 5


class Graph:
    """A `nodes` that knows some tokens and the decimals it read for them."""

    def __init__(self, known: dict[str, int]) -> None:
        self.known = {a.lower(): d for a, d in known.items()}

    def has(self, token: str) -> bool:
        return token.lower() in self.known

    def decimals(self, token: str) -> int:
        # 18 for anything else, exactly as the router's own does -- which is
        # why `has` has to be the test rather than this.
        return self.known.get(token.lower(), 18)


def warmed(page, graph: Graph) -> None:
    held = page.host._held
    held.session = type("Warm", (), {"nodes": graph})()
    held.stage = Stage.READY


def test_a_vault_no_pool_holds_still_reaches_the_picker():
    """sreUSD is $27.7M of assets and trades in nothing, so the pool list --
    which is where the pickers come from -- has never heard of it."""
    from router.session import chain_for

    vaults = chain_for(1).unlisted_vaults
    assert vaults, "the router names at least one of these on ethereum"
    address, symbol = vaults[0]

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page._coins = list(coins)
    warmed(page, Graph({address: 6}))

    page._offer_unlisted(1)

    added = [coin for coin in page._coins if coin.symbol == symbol]
    assert len(added) == 1
    assert added[0].address == address.lower()
    assert added[0].decimals == 6, "what the warm read, not a guess at 18"


def test_a_vault_the_graph_never_took_is_left_out():
    """`decimals` answers 18 for a token it has not met, and a wrong 18 on
    the selling side is a factor of a million."""
    from router.session import chain_for

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page._coins = list(coins)
    warmed(page, Graph({}))

    page._offer_unlisted(1)

    assert len(page._coins) == len(coins)
    assert all(symbol not in {c.symbol for c in page._coins}
               for _address, symbol in chain_for(1).unlisted_vaults)


def test_what_is_typed_is_priced_under_the_box():
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])
    view.set_prices({coins[0].address: 1.0005, coins[1].address: 0.9998})

    view.amount.value = "100000"
    view.receive.value = "99,987.65"
    view._sync_worth()

    assert view.sell_worth.value == "~ $100.05k"
    assert view.buy_worth.value == "~ $99.97k"
    assert view.sell_worth.visible and view.buy_worth.visible


def test_a_coin_with_no_price_says_nothing_rather_than_zero():
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])
    view.set_prices({coins[1].address: 1.0})

    view.amount.value = "5"
    view.receive.value = ""
    view._sync_worth()

    assert view.sell_worth.value == "", "no price for the sell coin"
    assert not view.sell_worth.visible
    assert view.buy_worth.value == "", "nothing typed on the buy side"


def test_an_unfinished_number_is_not_a_price():
    """Someone typing `0.` has not typed a number yet, and a line that reads
    `~ $0` under it would be answering before they finished."""
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])
    view.set_prices({coins[0].address: 2.0})

    for text in ("", ".", "0.", "abc", "0"):
        view.amount.value = text
        view._sync_worth()
        assert view.sell_worth.value == "", f"{text!r} priced as something"


def test_leaving_the_coin_list_puts_the_coin_back_in_the_box():
    """Opening blanks the box so typing does not land inside the symbol, and
    every way out has to undo that.  Dismissed with Escape it did not, and the
    box showed its hint beside a mark and a rate for the coin still being
    sold."""
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])

    view.sell._opened(Typed())
    assert view.sell.value == "", "blanked, so typing starts clean"

    view.sell._left(Typed())

    assert view.sell.value == "USDC"
    assert view.sell.picked is coins[0], "nothing was chosen either way"


def test_return_takes_the_coin_the_typing_was_pointing_at():
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])

    view.sell._opened(Typed())
    view.sell._typed(Typed("usdt"))
    view.sell._entered(Typed("usdt"))

    assert view.sell.picked is coins[1]
    assert view.sell.value == "USDT"


def test_return_reads_what_was_typed_even_if_the_event_forgot_it():
    """`on_submit` need not carry the box's contents, and picking the top of
    an unfiltered list would choose the busiest coin on the chain instead of
    the one that was typed."""
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])

    view.sell._opened(Typed())
    view.sell._typed(Typed("usdt"))
    view.sell._entered(Typed())

    assert view.sell.picked is coins[1]


def test_return_on_an_empty_box_chooses_nothing():
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])

    view.sell._opened(Typed())
    view.sell._entered(Typed())

    assert view.sell.picked is coins[0]
    assert view.sell.value == "USDC"


def test_return_on_a_query_that_names_nothing_changes_no_coin():
    coins = two_coins()
    view = view_with(coins)
    view.set_pair(coins[0], coins[1])

    view.sell._opened(Typed())
    view.sell._typed(Typed("zzz"))
    view.sell._entered(Typed("zzz"))

    assert view.sell.picked is coins[0]
    assert view.sell.value == "USDC"


class Session:
    """A session that records what `plan_call` was told."""

    solver = "rust"
    block = 100
    #: What `_gas_from_table` hands the router.  None is the router's own
    #: fallback -- per-kind medians rather than per-pool measurements.
    gas_table = None

    def __init__(self) -> None:
        self.planned: list[int] = []
        self.budgets: list[float | None] = []
        self.floors: list[float] = []
        self.drawn: list[int | None] = []

    def diagram(self, _result, *, verified_out=None):
        self.drawn.append(verified_out)

        class Empty:
            buses = elements = order = ()
        return Empty()

    async def plan_call(self, _result, *, receiver, sender, not_before=0,
                        slippage_bp=None, min_out_bp=0.0):
        self.planned.append(not_before)
        self.budgets.append(slippage_bp)
        self.floors.append(min_out_bp)

        class Plan:
            to = "0x" + "22" * 20
            data = b""
            value = 0
            token_in = "0x" + "33" * 20
            amount_in = 1
            quoted_out = guaranteed_out = 1
            tolerance_bp = 0.0
            gas = 0
            block = 100
            unbounded = ()
            reverted = ""
            gas_estimated = False

        return Plan()


async def test_a_plan_is_told_the_block_an_approval_landed_in():
    """The router reads through a load balancer, which is many nodes at
    slightly different heights.  One still behind cannot see an approval that
    has already happened, and the dry run then reverts on an allowance that
    is there."""
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    session = Session()
    # `RouterHost.session` reads through to the chain it is holding, so the
    # fake goes where the real one would be kept.
    held = page.host._held           # creates the entry for the current chain
    held.session = session
    held.stage = Stage.READY

    class Route:
        legs = ()

    class Result:
        route = Route()
        verified_out = 1
        amount_in = 1
        price_impact_bp = 0.0

    page._quote = type("Quote", (), {"result": Result()})()

    await page._plan_now()
    assert session.planned == [0], "nothing confirmed yet, so no floor"

    page._floor_block = 25_813_900
    await page._plan_now()
    assert session.planned[-1] == 25_813_900


async def test_the_picture_ends_on_the_number_in_the_buy_box():
    """The last bus is labelled with what the model accumulated unless it is
    handed the chain's own answer -- and the model's total counts every
    arrival, including flow that leaves again."""
    page = swap_page_with(Wallet(), two_coins())
    session = Session()
    held = page.host._held
    held.session = session
    held.stage = Stage.READY

    class Route:
        legs = ()

    class Result:
        route = Route()
        verified_out = 15_630_256_884_000_000_000_000
        amount_in = 1
        price_impact_bp = 0.0

    page._quoted(type("Quote", (), {"result": Result()})())
    assert session.drawn == [Result.verified_out]


def test_a_route_is_priced_by_its_shape_when_no_balance_can_be_simulated():
    """`_estimate_gas` grants the approval and never a balance, so nobody
    without a wallet gets a figure from the dry run -- and they are exactly
    the people deciding whether the trade is worth it."""
    from erouter.core.types import ArcKind

    class Leg:
        kind = ArcKind.SWAP_STABLE
        target = "0x" + "aa" * 20
        i, j = 0, 1

    class Realized:
        leg = Leg()

    class Route:
        legs = (Realized(), Realized())

    class Result:
        route = Route()

    page = swap_page_with(Wallet(), two_coins())
    held = page.host._held
    held.session = Session()
    held.stage = Stage.READY
    page._quote = type("Quote", (), {"result": Result()})()

    gas = page._gas_from_table()

    assert gas > 100_000, "two legs, a transaction and the split overhead"


def test_nothing_to_price_costs_nothing():
    page = swap_page_with(Wallet(), two_coins())
    assert page._gas_from_table() == 0, "no session, no route, no figure"


async def test_the_floor_only_ever_moves_forward():
    """A later transaction in an earlier block would be a reorg, not a lag."""
    page = swap_page_with(Wallet(), two_coins())
    page._floor_block = 200

    page._floor_block = max(page._floor_block, 150)
    assert page._floor_block == 200


# -- picking the coin the other side already holds ---------------------------


def test_choosing_the_other_side_s_coin_swaps_the_pair():
    """Nobody asks to swap USDC for USDC.

    Picking the coin the other selector holds is the pair the other way
    round, which is what the flip button does -- so the other selector takes
    what this one was showing rather than both ending up the same.
    """
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)
    assert page.view.pair == (usdc, usdt)

    page.view.sell.pick(usdt)           # sell := the coin buy already holds

    assert page.view.pair == (usdt, usdc), "it swapped rather than doubled"


def test_it_works_from_the_buying_side_too():
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)

    page.view.buy.pick(usdc)            # buy := the coin sell already holds

    assert page.view.pair == (usdt, usdc)


def test_an_unrelated_coin_leaves_the_other_side_alone():
    coins = two_coins()
    usdc, usdt = coins
    third = type(usdc)("0x" + "33" * 20, "DAI", "Dai", 18, 0.0, 0)
    page = swap_page_with(Wallet(), coins)

    page.view.sell.pick(third)

    assert page.view.pair == (third, usdt), "the buying side moved for no reason"


# -- what a swap of ours changes about this wallet --------------------------


class Sending:
    """A router contract with no chain behind it."""

    can_send = True

    def __init__(self) -> None:
        self.sent: list = []

    async def execute(self, plan) -> str:
        self.sent.append(plan)
        return "0x" + "ef" * 32

    async def needs_approval(self, _plan) -> bool:
        return False

    async def balance_of(self, _token: str) -> int:
        return 5_000_000


class Planned:
    """Enough of an execution plan for the send path to read."""

    def __init__(self) -> None:
        self.reverted = ""
        self.to = "0x" + "22" * 20
        self.token_in = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        self.amount_in = 1_000
        self.value = 0
        self.data = b""


async def test_a_swap_re_reads_both_balances_and_the_picker_order():
    """The swap moved this wallet, not just the pools.

    Both boxes print a balance as their hint, and the selling picker is
    ordered by those balances with the figures beside them -- so a swap that
    leaves either alone is showing what was true just before the transaction
    this tab sent.
    """
    import asyncio

    coins = two_coins()
    wallet = Wallet()
    wallet.connect("0x" + "11" * 20, **{coins[0].address: 5_000_000})
    page = swap_page_with(wallet, coins)

    scheduled = []
    page._page.run_task = lambda fn, *a: scheduled.append(fn)
    read = []
    original = page._read_balances

    async def watched():
        read.append(True)
        await original()

    page._read_balances = watched
    page._plan = Planned()
    contract = Sending()
    page._contract = lambda: contract
    page._confirm = lambda *a, **k: asyncio.sleep(0, result=0)

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()

    await page._swap()

    assert read, "the boxes still show the balance from before the swap"
    assert page._rank_by_holdings in scheduled, (
        "the selling picker kept its pre-swap order and figures"
    )


# -- the button between the two boxes ---------------------------------------


def test_flipping_carries_the_worked_out_amount_across():
    """Selling 1,000 USDT for 0.01 WBTC and flipping asks to sell the 0.01.

    Carrying the typed number across instead would ask to sell a thousand
    WBTC, which is a different question by four orders of magnitude and the
    one the widget used to put on screen.
    """
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)
    page.view.amount.value = "1000"
    page.view.receive.value = "0.010000"

    page.view._flip_clicked(None)

    assert page.view.pair == (usdt, usdc), "the coins turned round"
    assert page.view.amount.value == "0.010000", "the amounts did not follow"
    assert page.view.receive.value == "", "the output was guessed rather than quoted"


def test_flipping_with_nothing_quoted_keeps_what_was_typed():
    """With no output there is nothing better to offer than what was typed."""
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)
    page.view.amount.value = "1000"
    page.view.receive.value = ""

    page.view._flip_clicked(None)

    assert page.view.pair == (usdt, usdc)
    assert page.view.amount.value == "1000"


# -- the coins a pool holds, which its ribbon is drawn with -----------------


def pool_rows():
    return [{
        "address": "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7",
        "name": "Curve.fi DAI/USDC/USDT",
        "coins": [
            {"address": "0x6b175474e89094c44da98b954eedeac495271d0f",
             "symbol": "DAI", "decimals": 18},
            {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
             "symbol": "USDC", "decimals": 6},
        ],
    }]


def test_re_ordering_the_coins_keeps_the_pool_logos():
    """The ribbons lost their logos as soon as a wallet turned up.

    `offer` carries two unrelated things: which coins exist, and which pools
    hold what.  The callers that re-offer for *ordering* -- a wallet
    connecting, a swap of ours landing -- have no pool rows to hand over, and
    passing none used to empty the table, so every pool on the picture went
    back to being a name on its own for the rest of the session.
    """
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.offer(coins, "ethereum", pools=pool_rows())
    assert page.view.diagram._pool_coins, "the rows never arrived"

    page.view.offer(coins, "ethereum", owned=list(reversed(coins)))

    assert page.view.diagram._pool_coins, "re-ordering threw the logos away"
    held = page.view.diagram._pool_coins[
        "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"]
    assert [coin.symbol for coin in held] == ["DAI", "USDC"]


def test_a_different_chain_does_empty_it():
    """An address that means one pool here means nothing on the next network,
    and the wrong coins on a ribbon are worse than none."""
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.offer(coins, "ethereum", pools=pool_rows())

    page.view.diagram.set_chain("arbitrum")

    assert page.view.diagram._pool_coins == {}



# -- an amount is a count of one particular coin's units --------------------


def test_the_flip_writes_a_number_with_no_separators_in_it():
    """What goes in the box is read back as a number.

    A thousands separator is only ever in the way there, and in a locale
    where a comma *is* the decimal point it is worse than in the way.
    """
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.amount.value = "0.01"
    page.view.receive.value = "1,998,432.10"

    page.view._flip_clicked(None)

    assert page.view.amount.value == "1998432.10"


async def test_a_new_selling_coin_is_quoted_in_its_own_units():
    """2,000,000 USDC and 2,000,000 sDOLA are the same figure and a million
    million apart in units.

    The host is handed a raw count, and `set_pair` ends by quoting whatever it
    still holds -- so a pair change used to re-ask at the old coin's scale.
    Two million USDC read as sDOLA is two millionths of one, which does not
    route, and the refusal then cleared the amount so nothing quoted again.
    """
    from router.universe import CoinEntry

    usdc = CoinEntry("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "USDC",
                     "USD Coin", 6, 0.0, 0)
    sdola = CoinEntry("0x" + "5d" * 20, "sDOLA", "Staked DOLA", 18, 0.0, 0)
    page = swap_page_with(Wallet(), [usdc, sdola])
    page.view.set_pair(sdola, usdc)
    page.view.amount.value = "1998432.10"

    asked = []

    class Host:
        pair = None
        stage = Stage.READY

        def request(self, amount):
            asked.append(amount)

        async def set_pair(self, _src, _dst):
            return True

    page.host = Host()
    page._on_loading = lambda *_a: None
    page._on_loaded = lambda *_a: None

    async def no_balances():
        return None

    page._read_balances = no_balances

    await page._prepare(sdola.address, usdc.address)

    assert asked[0] == 0, "the old coin's count was left for set_pair to quote"
    assert asked[-1] == 1998432 * 10 ** 18 + 10 ** 17, (
        f"quoted at the wrong scale: {asked[-1]}"
    )


async def test_a_buy_chosen_while_the_router_is_busy_is_not_lost():
    """Reported from a live wallet: a swap set up as YB to USDC went out as YB
    to USDT, and the route drawn beside it was YB to USDT as well.

    A pair chosen while the router is not READY reaches neither `_prepare` nor
    `set_pair` -- both return on that stage, and neither leaves a note. Nothing
    re-issued it, so the widget said USDC while the quote, the route and the
    calldata all came from `held.pair`, which was still the coin before it.
    """
    import asyncio

    from router.universe import CoinEntry

    yb = CoinEntry("0x" + "11" * 20, "YB", "YieldBasis", 18, 0.0, 0)
    usdc, usdt = two_coins()
    page = swap_page_with(Wallet(), [yb, usdc, usdt])

    running: list = []

    class Page:
        def run_task(self, fn, *a):
            running.append(asyncio.ensure_future(fn(*a)))

    page._page = Page()

    class Host:
        stage = Stage.READY
        pair = None

        def request(self, _amount):
            pass

        async def set_pair(self, src, dst):
            if self.stage is not Stage.READY:
                return False            # exactly what `RouterHost` answers
            self.pair = (src.lower(), dst.lower())
            return True

    page.host = Host()
    page._on_loading = lambda *_a: None
    page._on_loaded = lambda *_a: None
    page._read_balances = lambda: _answer(None)
    page._remember_pair = lambda: _answer(None)

    async def settle():
        while running:
            batch = running[:]
            del running[:]
            await asyncio.gather(*batch)

    page.view.set_pair(yb, usdt)
    page._pair_changed()
    await settle()
    assert page.host.pair == (yb.address, usdt.address)

    page.host.stage = Stage.WARMING     # a refresh, a re-warm, a wallet switch
    page.view.set_pair(yb, usdc)        # and the widget now says USDC
    page._pair_changed()
    await settle()

    page.host.stage = Stage.READY
    page._stage_changed(Stage.READY, "")
    await settle()

    assert page.view.pair[1].symbol == "USDC"
    assert page.host.pair == (yb.address, usdc.address), (
        "the router was left holding the coin from before"
    )


async def test_a_router_holding_nothing_is_given_the_pair_on_screen():
    """A refresh whose re-preparation failed leaves the router with no pair at
    all, and nothing quotes without one. Reaching READY hands it the widget's
    rather than waiting for someone to change a coin."""
    import asyncio

    usdc, usdt = two_coins()
    page = swap_page_with(Wallet(), [usdc, usdt])

    running: list = []

    class Page:
        def run_task(self, fn, *a):
            running.append(asyncio.ensure_future(fn(*a)))

    page._page = Page()

    class Host:
        stage = Stage.READY
        pair = None             # dropped, and never put back

        def request(self, _amount):
            pass

        async def set_pair(self, src, dst):
            self.pair = (src.lower(), dst.lower())
            return True

    page.host = Host()
    page._on_loading = lambda *_a: None
    page._on_loaded = lambda *_a: None
    page._read_balances = lambda: _answer(None)

    page.view.set_pair(usdc, usdt)
    page._stage_changed(Stage.READY, "")
    while running:
        batch = running[:]
        del running[:]
        await asyncio.gather(*batch)

    assert page.host.pair == (usdc.address, usdt.address)


async def test_opening_a_chain_prepares_the_pair_once():
    """The warm announces READY from inside `host.open`, with the pair nulled
    just before it says so -- so the reconcile sees nothing prepared. `open`
    prepares it itself a few lines later, and two preparations of the same pair
    is one probe run wasted on every chain opened."""
    usdc, usdt = two_coins()
    page = swap_page_with(Wallet(), [usdc, usdt])
    scheduled: list = []
    page._page.run_task = lambda fn, *a: scheduled.append(a)

    class Host:
        stage = Stage.READY
        pair = None

    page.host = Host()
    page.view.set_pair(usdc, usdt)

    page._opening = True                    # what `open` holds while it runs
    page._stage_changed(Stage.READY, "")

    assert scheduled == [], "the reconcile raced `open`'s own preparation"


async def test_a_plan_is_not_built_for_a_pair_that_is_not_on_screen():
    """The quote, the bounds and the calldata all come from the router's pair,
    so a plan built while it disagrees with the widget buys a coin nobody
    chose. Refused rather than shipped."""
    page, session = swap_page_with_session()
    usdc, usdt = two_coins()
    page.host._held.pair = (usdt.address, usdc.address)     # the other way round

    await page._plan_now()

    assert session.budgets == [], "priced a route for coins nobody chose"
    assert page._plan is None


async def test_a_swap_is_not_sent_for_a_pair_that_is_not_on_screen():
    """The last gate before a signature: nothing goes to the wallet while the
    router is holding a pair the widget is not showing."""
    page = swap_page_with(Wallet(), two_coins())
    usdc, usdt = two_coins()
    sent = Sending()
    page._contract = lambda: sent
    page._plan = Planned()
    priced: list[int] = []

    async def replan():
        priced.append(1)

    page._plan_now = replan
    page._page.run_task = lambda fn, *a: None
    page._confirm = lambda *a, **k: _answer(0)      # so a send that got through
    page._read_balances = lambda: _answer(None)     # would land, not raise

    class Host:
        pair = (usdt.address, usdc.address)     # the other way round
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            return 0        # so a send that got through finishes, not raises

    page.host = Host()

    await page._swap()

    assert priced == [], "it priced the route rather than refusing outright"
    assert sent.sent == [], "a transaction went out for the wrong coins"


# -- what waits in the frame before there is a route ------------------------


def test_the_frame_waits_with_a_sticker_and_no_caption():
    """From the first frame, not from the first quote.

    Nothing called `show` until a quote or a pair change, so a freshly opened
    tab sat through the whole warm -- twenty seconds, the longest anyone looks
    at this panel -- showing a line of grey text and nothing else.
    """
    page = swap_page_with(Wallet(), two_coins())

    assert page.view.diagram._meme.visible, "the frame opened empty"
    assert page.view.diagram._empty.value == "", (
        "a caption saying the route appears here, under a picture that is "
        "plainly not a route"
    )


def test_a_reason_is_said_without_a_joke_beside_it():
    """A picture next to a failure reads as being pleased about it."""
    page = swap_page_with(Wallet(), two_coins())

    page.view.diagram.say("This route could not be drawn.")

    assert page.view.diagram._meme.visible is False
    assert page.view.diagram._empty.value == "This route could not be drawn."


def test_going_back_to_waiting_asks_for_another(monkeypatch):
    from ui import swap as swap_module

    asked = []

    def counted() -> str:
        asked.append(1)
        return "memes/001.webp"

    monkeypatch.setattr(swap_module.assets, "meme", counted)
    page = swap_page_with(Wallet(), two_coins())
    diagram = page.view.diagram
    before = len(asked)

    diagram.say("nothing to draw")
    diagram._show_meme(True)

    assert len(asked) == before + 1, "it put the same one back up"
    assert diagram._meme.visible


def test_staying_empty_keeps_the_same_one(monkeypatch):
    """Picking again on every redraw turns a thing to look at into a
    thing that flickers -- and `show(None)` runs on every keystroke."""
    from ui import swap as swap_module

    asked = []

    def counted() -> str:
        asked.append(1)
        return "memes/001.webp"

    monkeypatch.setattr(swap_module.assets, "meme", counted)
    page = swap_page_with(Wallet(), two_coins())
    diagram = page.view.diagram
    before = len(asked)

    diagram._show_meme(True)
    diagram._show_meme(True)

    assert len(asked) == before, "it re-picked while nothing had changed"


# -- and not flashing in the gap between two routes -------------------------


class Looping:
    """A page whose `run_task` actually runs the coroutine."""

    def __init__(self) -> None:
        self.tasks: list = []

    def run_task(self, handler, *args):
        import asyncio

        task = asyncio.ensure_future(handler(*args))
        self.tasks.append(task)
        return task


async def test_a_route_going_away_does_not_flash_a_sticker():
    """The flip empties the frame and fills it again as soon as the new quote
    lands.  A picture in the few hundred milliseconds between the two is a
    flash, not something to look at."""
    import asyncio

    from ui import swap as swap_module

    page = swap_page_with(Wallet(), two_coins())
    diagram = page.view.diagram
    diagram._page = Looping()
    diagram._meme.visible = False
    diagram._diagram = object()             # a route is showing

    monkeyish = swap_module.MEME_AFTER
    try:
        swap_module.MEME_AFTER = 0.05
        diagram.show(None)                  # the flip empties it
        assert diagram._meme.visible is False, "it went up straight away"
        assert diagram._empty.value == "", "the caption flashed instead"

        diagram._diagram = object()         # the new quote lands first
        await asyncio.sleep(0.12)
        assert diagram._meme.visible is False, "it went up behind the route"
    finally:
        swap_module.MEME_AFTER = monkeyish


async def test_but_it_does_go_up_if_no_route_arrives():
    import asyncio

    from ui import swap as swap_module

    page = swap_page_with(Wallet(), two_coins())
    diagram = page.view.diagram
    diagram._page = Looping()
    diagram._meme.visible = False
    diagram._diagram = object()

    monkeyish = swap_module.MEME_AFTER
    try:
        swap_module.MEME_AFTER = 0.05
        diagram.show(None)
        await asyncio.sleep(0.12)
        assert diagram._meme.visible, "the frame was left blank"
        assert diagram._empty.value == ""
    finally:
        swap_module.MEME_AFTER = monkeyish


# -- switching network ------------------------------------------------------


async def test_a_new_network_takes_the_old_one_off_the_screen():
    """The amount counts a coin that is not in the new list, the figures were
    quoted against pools that are not on it, and the route drawn belongs to
    the network being left -- and the warm ahead is twenty seconds, which is
    a long time to show somebody the wrong network's answer."""
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.chain_id_now = 1
    page.view.amount.value = "2000"
    page.view.receive.value = "1999.5"
    page.view.diagram._diagram = object()
    page._quote = object()
    page._plan = Planned()
    page._balances = {coins[0].address: 5}

    page._chain_id = lambda: _answer(100)
    page._chain_name = lambda: "gnosis"
    page._offer_coins = lambda _id: _answer(None)
    page._read_balances = lambda: _answer(None)
    page._backend_error = "no backend here"

    await page.open()

    assert page.view.amount.value == "", "the old network's amount stayed"
    assert page.view.receive.value == "", "the old network's figure stayed"
    assert page.view.diagram._diagram is None, "the old network's route stayed"
    assert page._quote is None and page._plan is None
    assert page._balances == {}


async def test_the_frame_waits_with_a_picture_not_the_old_route():
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.diagram._diagram = object()
    page.view.diagram._meme.visible = False

    page.view.forget_chain()

    assert page.view.diagram._meme.visible, "it left the frame blank"
    assert page.view.diagram._empty.value == ""


def _answer(value):
    import asyncio

    return asyncio.sleep(0, result=value)


# -- what a chain opens on --------------------------------------------------


async def test_a_chain_does_not_open_on_a_wrapping():
    """The gas token is listed beside its own wrapper, and the default pair
    is the two busiest coins -- so gnosis would have opened on XDAI to WXDAI,
    which is one for one, for ever, with no rate to show."""
    from router.universe import NATIVE, CoinEntry

    wxdai = "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
    coins = [
        CoinEntry(NATIVE, "XDAI", "xDAI", 18, 900.0, 3),
        CoinEntry(wxdai, "WXDAI", "Wrapped xDAI", 18, 900.0, 3),
        CoinEntry("0x" + "11" * 20, "USDC.e", "USD Coin", 6, 500.0, 2),
    ]
    page = swap_page_with(Wallet(), coins)
    page._remembered_pair = lambda _id: _answer(None)

    await page._open_pair(100, coins)

    sell, buy = page.view.pair
    assert sell.symbol == "XDAI"
    assert buy.symbol == "USDC.e", f"opened on a wrapping: XDAI -> {buy.symbol}"


def wrapping_page():
    """A page on Ethereum showing WETH -> ETH, with the host prepared for the
    routed pair that came before it -- which is the state every wrapping is
    reached from."""
    from router.universe import NATIVE, CoinEntry

    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    coins = [
        CoinEntry(NATIVE, "ETH", "Ether", 18, 900.0, 3),
        CoinEntry(weth, "WETH", "Wrapped Ether", 18, 900.0, 3),
        CoinEntry(usdt, "USDT", "Tether", 6, 500.0, 2),
    ]
    page = swap_page_with(Wallet(), coins)
    page.view.set_pair(coins[1], coins[0])          # WETH -> ETH

    class Host:
        stage = Stage.READY
        pair = (usdt, weth)                          # what it last routed
        session = None

        def __init__(self) -> None:
            self.asked: list[int] = []

        def request(self, amount=0):
            self.asked.append(amount)

    page.host = Host()
    return page, coins


async def test_a_wrapping_is_never_a_stale_pair():
    """Reported: WETH -> ETH drew 1 WETH = 2,419.64 ETH over five pools, with
    a dead Swap button and "Re-pricing for the coins you chose." under it.

    A wrapping never goes near the host -- `wrapping` answers it before the
    warm is -- so `host.pair` keeps whichever routed pair came before, and
    comparing the two blindly called every wrap stale for ever.  Nothing
    could then be planned, sent, or re-priced when the slippage changed.
    """
    page, _coins = wrapping_page()

    assert page._wrapping() is not None, "WETH -> ETH is a wrapping"
    assert page._pair_is_stale() is False


async def test_a_wrapping_stops_the_host_quoting_the_pair_it_left():
    """The host goes on answering for what it is prepared for, and a wrapping
    never re-prepares it -- so the answer for the pair just left arrives and
    is drawn against the coins now on screen."""
    page, _coins = wrapping_page()
    page._remember_pair = lambda: _answer(None)

    page._pair_changed()

    assert page.host.asked == [0], "the host was left quoting the old pair"


async def test_a_quote_for_another_pair_is_not_drawn_over_a_wrapping():
    """The same answer arriving a moment later, after the pair has changed.
    Drawn, it is 1 WETH = 2,419.64 ETH."""
    page, _coins = wrapping_page()
    drawn: list = []
    page.view.show_quote = lambda quote, plan=None: drawn.append(quote)

    class Result:
        route = None
        verified_out = 2419
        amount_in = 1
        price_impact_bp = 3.44

    page._quoted(type("Quote", (), {"result": Result()})())

    assert drawn == [], "an answer about other coins reached the screen"
    assert page._quote is None


async def test_a_remembered_wrapping_is_still_honoured():
    """Choosing one is fine; it is only a poor thing to *open* on."""
    from router.universe import NATIVE, CoinEntry

    wxdai = "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
    coins = [
        CoinEntry(NATIVE, "XDAI", "xDAI", 18, 900.0, 3),
        CoinEntry(wxdai, "WXDAI", "Wrapped xDAI", 18, 900.0, 3),
        CoinEntry("0x" + "11" * 20, "USDC.e", "USD Coin", 6, 500.0, 2),
    ]
    page = swap_page_with(Wallet(), coins)
    page._remembered_pair = lambda _id: _answer((NATIVE, wxdai))

    await page._open_pair(100, coins)

    assert [c.symbol for c in page.view.pair] == ["XDAI", "WXDAI"]


# -- a pool's name on a narrow picture --------------------------------------


def a_band(label: str, detail: str, x0: float, x1: float):
    """One ribbon running straight between two columns."""
    from ui.routegraph import Band

    return Band(index=0, label=label, kind="SWAP_STABLE", share=1.0,
                points=((x0, 100.0), (x1, 100.0)), height=40.0, detail=detail)


def test_the_marks_go_before_the_name_does():
    """The stack is the ornament and the name is what is being read.

    A fifteen-leg route in a 720-point panel had eleven names dropped for
    width -- and six of those fit once the coin stack came off, which is the
    difference between a picture that says where the money went and one that
    does not.
    """
    from curve.models import Coin

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    diagram = page.view.diagram
    pool = "0x" + "ab" * 20
    diagram._pool_coins = {pool: [
        Coin(address="0x" + "11" * 20, symbol="DOLA", decimals=18),
        Coin(address="0x" + "22" * 20, symbol="sUSDe", decimals=18),
    ]}
    # Room for the name and not for the name plus two marks.
    band = a_band("DOLA/sUSDe", pool, 300.0, 400.0)

    drawn = diagram._leg_row([band], 720.0, [])

    assert len(drawn) == 1, "the name was dropped rather than the marks"


def test_a_name_that_will_not_fit_at_all_is_still_dropped():
    """There are eighteen-leg routes, and a name wider than its ribbon put
    over the top of everything is not a picture of anything."""
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    diagram = page.view.diagram
    pool = "0x" + "cd" * 20
    diagram._pool_coins = {}
    band = a_band("Curve Strategic Ethena Reserves", pool, 300.0, 330.0)

    assert diagram._leg_row([band], 720.0, []) == []


def test_the_marks_stay_where_there_is_room_for_them():
    from curve.models import Coin

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    diagram = page.view.diagram
    pool = "0x" + "ef" * 20
    diagram._pool_coins = {pool: [
        Coin(address="0x" + "11" * 20, symbol="DOLA", decimals=18),
        Coin(address="0x" + "22" * 20, symbol="sUSDe", decimals=18),
    ]}
    band = a_band("DOLA/sUSDe", pool, 100.0, 600.0)

    drawn = diagram._leg_row([band], 720.0, [])

    assert len(drawn) == 1
    import flet as ft
    assert isinstance(drawn[0].content.content, ft.Row), "the marks were dropped"


# -- and the marks shrink to what is left ------------------------------------


def test_marks_take_the_room_left_beside_the_name():
    from ui.swap import POOL_MARK, _marks_that_fit, _stack_width

    plenty = _marks_that_fit(2, _stack_width(2) + 20.0)

    assert plenty == POOL_MARK, "it drew them smaller than it had to"


def test_marks_shrink_rather_than_go():
    """Taking them off to make a name fit left the room they wanted empty --
    fifteen to twenty-five points of it, which is a smaller stack."""
    from ui.swap import POOL_MARK, POOL_MARK_MIN, _marks_that_fit, _stack_width

    tight = _stack_width(2) - 4.0
    mark = _marks_that_fit(2, tight)

    assert POOL_MARK_MIN <= mark < POOL_MARK
    assert _stack_width(2, mark) <= tight, "the smaller stack still did not fit"


def test_marks_go_when_they_would_be_dots():
    """Below the floor a token mark says a pool has coins and nothing about
    which, which is not worth the room."""
    from ui.swap import _marks_that_fit

    assert _marks_that_fit(2, 8.0) == 0.0


def test_a_pool_with_no_coins_asks_for_nothing():
    from ui.swap import _marks_that_fit

    assert _marks_that_fit(0, 100.0) == 0.0


def test_a_late_vault_does_not_take_the_balances_off_the_picker():
    """The unlisted vaults arrive after the warm, twenty seconds behind the
    holdings that were ranked while the bar was still moving.  Handing `offer`
    the plain list there puts the selling side back in volume order -- and the
    balances ride on the ranked entries, so every one of them goes with it,
    which reads as a wallet that is not connected.
    """
    from dataclasses import replace

    from router.session import chain_for

    address, symbol = chain_for(1).unlisted_vaults[0]
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page._coins = list(coins)
    # What `_rank_by_holdings` hands over once the balances are in.
    owned = [replace(coins[1], balance=5_000_000, worth=5.0), coins[0]]
    page._owned = owned          # what `_rank_by_holdings` leaves behind
    page.view.offer(coins, "ethereum", owned=owned)
    assert any(e.balance for e in page.view.sell._entries), "balances to lose"

    warmed(page, Graph({address: 6}))
    page._offer_unlisted(1)

    assert any(coin.symbol == symbol for coin in page._coins), "the vault landed"
    held = {e.address: e.balance for e in page.view.sell._entries}
    assert held.get(coins[1].address) == 5_000_000, (
        "the vault arriving must not blank what the wallet holds")


def _ranked_with(page, coins, monkeypatch, *, on_read=None):
    """Drive `_rank_by_holdings` with the balances stubbed at the multicall."""
    async def read_balances(_provider, _owner, _coins):
        if on_read is not None:
            on_read()
        return {coins[1].address: 5_000_000}

    monkeypatch.setattr("ui.swap_page.holdings.read_balances", read_balances)

    class Prices:
        async def usd_prices(self, _chain):
            return {c.address: 1.0 for c in coins}

    page._api = Prices()


def test_a_ranking_for_the_old_wallet_is_dropped_when_the_account_moved(monkeypatch):
    """Switching wallets starts a second ranking while the first is still in
    flight, and the two race.  The slower one carries the *previous* account's
    balances, so applying it would show one wallet's holdings under another's
    address.
    """
    import asyncio

    coins = two_coins()
    wallet = Wallet()
    wallet.connect("0x" + "a" * 40)
    page = swap_page_with(wallet, coins)
    page._coins = list(coins)
    # The switch lands while the balances are being read, which is exactly the
    # window a wallet change opens.
    _ranked_with(page, coins, monkeypatch,
                 on_read=lambda: setattr(wallet, "address", "0x" + "b" * 40))

    asyncio.run(page._rank_by_holdings())

    assert page._owned is None, "a ranking for the account we left is not ours"
    assert all(e.balance == 0 for e in page.view.sell._entries)


def test_a_ranking_for_the_current_wallet_is_kept(monkeypatch):
    """The same path with nothing moving under it still has to land."""
    import asyncio

    coins = two_coins()
    wallet = Wallet()
    wallet.connect("0x" + "a" * 40)
    page = swap_page_with(wallet, coins)
    page._coins = list(coins)
    _ranked_with(page, coins, monkeypatch)

    asyncio.run(page._rank_by_holdings())

    assert page._owned is not None
    assert any(e.balance for e in page.view.sell._entries)


def test_switching_wallets_drops_the_old_ones_balances_at_once(monkeypatch):
    """Balances belong to an address.  Shown under a different one they are
    not stale, they are wrong -- so they go the moment the account does,
    before anything has had a chance to read the new wallet's.
    """
    import asyncio
    from dataclasses import replace

    coins = two_coins()
    wallet = Wallet()
    wallet.connect("0x" + "a" * 40)
    page = swap_page_with(wallet, coins)
    page._coins = list(coins)
    # Where the first wallet left things: a ranking, and a balance MAX reads.
    page._owned = [replace(coins[1], balance=5_000_000, worth=5.0), coins[0]]
    page.view.offer(coins, "ethereum", owned=page._owned)
    page._balances = {coins[1].address: 5_000_000}

    # The second wallet, with nothing read for it yet.
    wallet.address = "0x" + "b" * 40
    monkeypatch.setattr("ui.swap_page.holdings.read_balances",
                        _never_answers)
    asyncio.run(page.wallet_changed())

    assert page._owned is None
    assert 5_000_000 not in page._balances.values(), (
        "MAX must not offer the old wallet's amount")
    assert all(e.balance == 0 for e in page.view.sell._entries)


async def _never_answers(_provider, _owner, _coins):
    """A wallet whose balances have not come back yet, or at all."""
    return {}


def test_a_wallet_holding_none_of_them_shows_none_of_them(monkeypatch):
    """The case that used to keep the previous wallet's list: an account that
    holds nothing on this chain reads as an empty answer, and `_rank_by_
    holdings` returns without drawing.  It has nothing to undo now.
    """
    import asyncio
    from dataclasses import replace

    coins = two_coins()
    wallet = Wallet()
    wallet.connect("0x" + "a" * 40)
    page = swap_page_with(wallet, coins)
    page._coins = list(coins)
    page._owned = [replace(coins[1], balance=5_000_000, worth=5.0), coins[0]]
    page.view.offer(coins, "ethereum", owned=page._owned)

    wallet.address = "0x" + "b" * 40
    monkeypatch.setattr("ui.swap_page.holdings.read_balances", _never_answers)
    asyncio.run(page.wallet_changed())
    asyncio.run(page._rank_by_holdings())

    assert all(e.balance == 0 for e in page.view.sell._entries), (
        "an empty answer for the new wallet must not leave the old one's")


def _watch_focus(view):
    """Record `focus()` calls on the amount box.  It is a coroutine in Flet."""
    calls = []

    async def focus():
        calls.append(True)

    view.amount.focus = focus
    return calls


def test_the_caret_stays_in_the_box_when_the_warm_lands_mid_number():
    """The warm ends whenever it ends, and every redraw behind it rebuilds the
    subtree the amount box lives in.  Somebody typing through that has to keep
    the caret, or the next keystroke goes nowhere.
    """
    import asyncio

    import flet as ft

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    view = page.view
    view._took_caret()
    view.amount.value = "100"
    view._caret_moved(type("E", (), {"selection": ft.TextSelection(3, 3)})())

    held = view.caret()
    calls = _watch_focus(view)
    asyncio.run(view.restore_caret(held))

    assert calls, "the box has to be focused again"
    assert view.amount.selection.base_offset == 3, "and at the same offset"


def test_a_caret_is_not_put_back_over_what_was_typed_since():
    """More arrived between the snapshot and the redraw finishing.  Restoring
    the old offset would drop the reader mid-number instead of where they are.
    """
    import asyncio

    import flet as ft

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    view = page.view
    view._took_caret()
    view.amount.value = "100"
    view._caret_moved(type("E", (), {"selection": ft.TextSelection(3, 3)})())
    held = view.caret()

    view.amount.value = "10050"          # typed on while the warm finished
    calls = _watch_focus(view)
    asyncio.run(view.restore_caret(held))

    assert calls, "still focused"
    assert view.amount.selection == ft.TextSelection(5, 5), (
        "the end of the newer number, not the offset they have already left")


def test_a_reader_who_was_not_typing_is_left_alone():
    """Nobody in the box means nobody to put back, and stealing focus into it
    would be worse than the bug.
    """
    import asyncio

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    view = page.view
    held = view.caret()
    calls = _watch_focus(view)

    asyncio.run(view.restore_caret(held))

    assert not calls


async def test_open_puts_the_caret_back_after_the_warm():
    """The whole path, not just the view: somebody typing while the bar moves
    is still in the box when `open` returns."""
    import flet as ft

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.chain_id_now = 1
    page._backend = object()
    page._chain_id = lambda: _answer(1)
    page._offer_coins = lambda _id: _answer(None)
    page._read_balances = lambda: _answer(None)
    page._prepare = lambda _s, _b: _answer(None)
    page.host.open = lambda _id: _answer(None)
    page.host._held.stage = Stage.READY

    # Mid-number when the warm lands.
    page.view._took_caret()
    page.view.amount.value = "12"
    page.view._caret_moved(type("E", (), {"selection": ft.TextSelection(2, 2)})())
    calls = _watch_focus(page.view)

    await page.open()

    assert calls, "the reader was left outside the box they were typing in"
    assert page.view.amount.value == "12", "and what they typed is still there"


async def test_open_leaves_the_caret_alone_when_nobody_was_typing():
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.chain_id_now = 1
    page._backend = object()
    page._chain_id = lambda: _answer(1)
    page._offer_coins = lambda _id: _answer(None)
    page._read_balances = lambda: _answer(None)
    page._prepare = lambda _s, _b: _answer(None)
    page.host.open = lambda _id: _answer(None)
    page.host._held.stage = Stage.READY
    calls = _watch_focus(page.view)

    await page.open()

    assert not calls, "the warm ending must not pull focus into the box"


def test_focus_alone_would_hand_back_the_whole_number_selected():
    """Focusing a field selects all of it.  Typing "500", having the warm
    land, then pressing "0" gave "0" -- the keystroke replaced the selection
    instead of extending the number.  `on_selection_change` reports where the
    caret went rather than that it moved with the text, so nothing tracked is
    the ordinary case and it is the one that used to select the lot.
    """
    import asyncio

    import flet as ft

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    view = page.view
    view._took_caret()
    view.amount.value = "500"          # typed, with no selection event behind it
    assert view._caret is None, "the case this is about"

    held = view.caret()
    _watch_focus(view)
    asyncio.run(view.restore_caret(held))

    assert view.amount.selection == ft.TextSelection(3, 3), (
        "a collapsed caret after the number, so the next key extends it")


def test_a_tracked_offset_past_the_end_is_not_used():
    """A stale offset longer than what is in the box would throw."""
    import asyncio

    import flet as ft

    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    view = page.view
    view._took_caret()
    view.amount.value = "5"
    view._caret_moved(type("E", (), {"selection": ft.TextSelection(9, 9)})())

    held = view.caret()
    _watch_focus(view)
    asyncio.run(view.restore_caret(held))

    assert view.amount.selection == ft.TextSelection(1, 1)


class Reverting(Planned):
    """A plan the dry run refused, at the block it was priced against."""

    def __init__(self, why: str = "leg below its minimum rate") -> None:
        super().__init__()
        self.reverted = why


async def test_a_revert_re_reads_the_state_and_tries_again():
    """The bound was set against slots that have since moved, which is what
    `leg below its minimum rate` means.  Re-reading them is the answer, and
    `plan_call` re-reads the route's own accounts at the newest block.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Reverting()
    planned: list[int] = []

    async def replan():
        planned.append(1)
        page._plan = Planned()          # fresh state, and now it goes through

    page._plan_now = replan
    page._read_balances = lambda: _answer(None)
    page._confirm = lambda *a, **k: _answer(0)
    page._page.run_task = lambda fn, *a: None

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert planned == [1], "it re-planned rather than refusing"
    assert page._plan is None, "sent, so the plan is spent"


async def test_a_second_refusal_still_leaves_the_tab_able_to_try():
    """One revert used to stop the tab sending anything ever after: the
    refused plan stayed put, and every later press read the same answer off
    it without asking the chain anything.
    """
    page = swap_page_with(Wallet(), two_coins())
    sent = Sending()
    page._contract = lambda: sent
    page._plan = Reverting()

    async def replan():
        page._plan = Reverting()        # the pool really has moved away

    page._plan_now = replan

    await page._swap()

    assert page._plan is None, "cleared, so the next press plans afresh"


class Failing(Sending):
    """A router whose transaction is mined and reverts."""

    def __init__(self, rejected: bool = False) -> None:
        self.rejected = rejected

    async def execute(self, _plan) -> str:
        # `rejected_by_user` is a property the class answers, so a rejection
        # is raised as the thing that really carries one.
        from wallet.base import USER_REJECTED_REQUEST, RpcError, WalletError

        if self.rejected:
            raise RpcError(USER_REJECTED_REQUEST, "User rejected the request")
        raise WalletError("The transaction was mined but reverted.")


class WatchedHost:
    pair = None
    session = None

    def __init__(self) -> None:
        self.refreshed = 0
        self.quoted: list = []
        self.floors: list[int] = []

    async def after_swap(self, not_before: int = 0) -> int:
        self.refreshed += 1
        self.floors.append(not_before)
        return 0

    def request(self, amount) -> None:
        self.quoted.append(amount)


async def test_a_revert_on_chain_sends_us_back_for_the_state():
    """A revert is the pool saying its state is not what the plan was priced
    against, so the state is what to re-read -- the same re-read a swap of
    ours triggers when it lands, and for the same reason.
    """
    page = swap_page_with(Wallet(), two_coins())
    contract = Failing()
    page._contract = lambda: contract
    page._plan = Planned()
    page._read_balances = lambda: _answer(None)
    host = WatchedHost()
    page.host = host

    await page._swap()

    assert host.refreshed == 1, "the slots were re-read"
    assert host.quoted, "and the quote priced against them"
    assert page._plan is None, "the plan it was priced from is spent"


async def test_a_wallet_rejection_re_reads_the_state_too():
    """It used to be left alone on the grounds that nothing had gone out, so
    nothing had moved.  But somebody who declined did so because their wallet
    told them it would fail, as often as not -- which is precisely when the
    state behind it is worth re-reading, and precisely when a stale one goes
    on producing the same doomed transaction.
    """
    page = swap_page_with(Wallet(), two_coins())
    contract = Failing(rejected=True)
    page._contract = lambda: contract
    page._plan = Planned()
    host = WatchedHost()
    page.host = host

    await page._swap()

    assert host.refreshed == 1, "the rejection taught it nothing"
    assert page._plan is None, "and the plan is stale either way"


async def test_the_tab_can_send_again_after_a_revert():
    """One revert on chain used to need the whole UI reloading."""
    page = swap_page_with(Wallet(), two_coins())
    reverting = Failing()
    page._contract = lambda: reverting
    page._plan = Planned()
    page._read_balances = lambda: _answer(None)
    page.host = WatchedHost()

    await page._swap()

    assert not page._sending, "not stuck mid-send"
    assert page._plan is None, "and the next press plans afresh"


def swap_page_with_session():
    """A page whose `plan_call` is recorded rather than sent anywhere."""
    page = swap_page_with(Wallet(), two_coins())
    session = Session()
    held = page.host._held          # where `RouterHost.session` reads from
    held.session = session
    held.stage = Stage.READY

    class Route:
        legs = ()

    class Result:
        route = Route()
        verified_out = 1
        amount_in = 1
        price_impact_bp = 0.0

    page._quote = type("Quote", (), {"result": Result()})()
    return page, session


async def test_the_automatic_rule_is_what_ships_by_default():
    """Naming a budget is the exception.  Left alone, every leg is bounded
    from its own pool's fee and the route's total is whatever those come to.
    """
    page, session = swap_page_with_session()

    await page._plan_now()

    assert page.view.slippage_bp is None
    assert session.budgets == [None]
    assert page.view.rows.said("slippage") == "auto"


async def test_a_named_budget_reaches_the_router():
    """`core.slippage` divides it between the legs -- once per path, so a
    route that branches spends it once however many legs a branch has."""
    page, session = swap_page_with_session()
    await page._plan_now()

    page.view._set_slippage(50.0)
    await page._plan_now()

    assert page.view.slippage_bp == 50.0
    assert session.budgets[-1] == 50.0
    assert page.view.rows.said("slippage") == "0.50%"


async def test_choosing_a_budget_re_plans_but_does_not_re_quote():
    """The bounds live on the plan, so the plan is what has to be built
    again; re-quoting would take the number on screen away for nothing."""
    page, _session = swap_page_with_session()
    await page._plan_now()
    assert page._plan is not None
    asked: list = []
    page._page.run_task = lambda fn, *a: asked.append(fn)

    page.view._set_slippage(30.0)

    assert page._plan is None, "the old bounds are not the new ones"
    assert asked == [page._plan_now], "re-planned, and nothing else"


async def test_going_back_to_auto_says_so():
    page, session = swap_page_with_session()
    page.view._set_slippage(100.0)
    await page._plan_now()

    page.view._set_slippage(None)
    await page._plan_now()

    assert session.budgets[-1] is None
    assert page.view.rows.said("slippage") == "auto"


def test_the_same_choice_twice_changes_nothing():
    """A menu that fires on every open would re-plan for a decision nobody
    took."""
    page, _session = swap_page_with_session()
    page.view._set_slippage(50.0)
    asked: list = []
    page._page.run_task = lambda fn, *a: asked.append(fn)

    page.view._set_slippage(50.0)

    assert asked == []


def test_the_slippage_row_reads_as_a_setting_before_any_quote():
    """The other four are figures and have nothing to say yet; this one is a
    setting and always does."""
    page = swap_page_with(Wallet(), two_coins())

    assert page.view.rows.said("slippage") == "auto"
    assert page.view.rows.said("impact") == "-"


def test_an_empty_box_means_auto():
    """Which is what an empty box should mean: the reader has taken their
    number back rather than named a new one."""
    from ui.swap import parse_slippage

    assert parse_slippage("") is None
    assert parse_slippage("   ") is None


def test_a_typed_percent_becomes_basis_points():
    from ui.swap import parse_slippage

    assert parse_slippage("0.5") == 50.0
    assert parse_slippage("1") == 100.0
    assert parse_slippage("0.05") == 5.0
    assert parse_slippage("0,5") == 50.0, "a decimal comma is a decimal point"
    assert parse_slippage("0.5%") == 50.0, "the suffix is already on the box"


def test_a_number_that_is_not_one_is_refused():
    import pytest as _pytest

    from ui.swap import parse_slippage

    for typed in ("abc", "1.2.3", "-", "one"):
        with _pytest.raises(ValueError):
            parse_slippage(typed)

    # A bare "%" is the suffix with no number in front of it, which is an
    # empty box wearing a sign -- so it reads as auto rather than as a fault.
    assert parse_slippage("%") is None


def test_zero_and_less_are_refused():
    """A budget of nothing is not a budget, and the router floors every leg
    at its rounding room anyway."""
    import pytest as _pytest

    from ui.swap import parse_slippage

    for typed in ("0", "-1", "-0.5"):
        with _pytest.raises(ValueError):
            parse_slippage(typed)


def test_a_typo_sized_number_is_refused():
    """5000 in a box meant for 0.5 is a route that sells at any price at all,
    and nobody means that."""
    import pytest as _pytest

    from ui.swap import MAX_SLIPPAGE_PCT, parse_slippage

    assert parse_slippage(str(MAX_SLIPPAGE_PCT)) == MAX_SLIPPAGE_PCT * 100
    with _pytest.raises(ValueError):
        parse_slippage(str(MAX_SLIPPAGE_PCT + 0.1))


def test_the_setting_goes_back_into_the_box_as_it_was_typed():
    from ui.swap import slippage_percent

    assert slippage_percent(None) == "", "auto leaves it empty"
    assert slippage_percent(50.0) == "0.5"
    assert slippage_percent(100.0) == "1"


def _view_with_a_route():
    """A view holding a quote, so Swap is offered at all."""
    page = swap_page_with(Wallet(), two_coins())
    view = page.view
    view._empty = False
    view._blocked = False
    view._sync_submit()
    return view


def test_swap_waits_for_the_allowance():
    """An unapproved token reverts on the `transferFrom` before it reaches a
    pool, so offering the button is offering a transaction that cannot land.
    """
    view = _view_with_a_route()
    assert not view.submit_button.disabled, "nothing in the way yet"

    view.show_approval(True)

    assert view.submit_button.disabled
    assert view.approve_button.visible
    assert view.submit_button.content == "2. Swap"


def test_swap_comes_back_when_the_approval_lands():
    view = _view_with_a_route()
    view.show_approval(True)

    view.show_approval(False)

    assert not view.submit_button.disabled
    assert not view.approve_button.visible
    assert view.submit_button.content == "Swap"


def test_an_approval_does_not_override_the_other_reasons():
    """Approved is one term of four, not the answer on its own."""
    view = _view_with_a_route()
    view.show_approval(False)
    view._empty = True

    view._sync_submit()

    assert view.submit_button.disabled, "no amount is still no swap"


def test_sending_still_locks_the_button():
    view = _view_with_a_route()
    view.show_approval(False)

    view.busy(True)

    assert view.submit_button.disabled
    assert view.approve_button.disabled


async def test_a_named_budget_also_bounds_the_whole_route():
    """The per-leg bounds are what protects each hop; `min_out` is the promise
    about the number on screen.  Somebody who said "no worse than 0.5%" has
    named exactly that, so the contract is told it too.
    """
    page, session = swap_page_with_session()
    page.view._set_slippage(50.0)

    await page._plan_now()

    assert session.budgets[-1] == 50.0
    assert session.floors[-1] == 50.0


async def test_the_automatic_rule_names_no_end_to_end_bound():
    """There is no figure to make one from: the total under the automatic
    rule is whatever the pools' own fees came to."""
    page, session = swap_page_with_session()

    await page._plan_now()

    assert session.budgets[-1] is None
    assert session.floors[-1] == 0.0


async def test_a_revert_re_sweeps_the_whole_state_not_just_the_route():
    """`plan_call` re-reads the route's own accounts and no others.  What
    moved a pool under a plan may be a slot nothing on the route touched, so
    the recovery is the full sweep the on-chain revert path already gets.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Reverting()
    order: list[str] = []

    async def replan():
        order.append("plan")
        # The press prices afresh; that price is refused, and only the one
        # taken after the sweep goes through.
        page._plan = Reverting() if order.count("plan") == 1 else Planned()

    page._plan_now = replan
    page._read_balances = lambda: _answer(None)
    page._confirm = lambda *a, **k: _answer(0)
    page._page.run_task = lambda fn, *a: None

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def refresh(self):
            order.append("sweep")
            return 0

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert order == ["plan", "sweep", "plan"], "swept, then priced against it"


async def test_a_refusal_that_outlives_its_block_is_taken_down_by_a_quote():
    """The tab used to read as broken long after it had recovered: the line
    sat there through every later quote.
    """
    page = swap_page_with(Wallet(), two_coins())
    page.view.say("This route would not go through: <min", FAILED)
    assert page.view.status.visible

    page._quoted(None)

    assert not page.view.status.visible, "a fresh answer overtakes it"


async def test_a_quote_landing_behind_a_pending_line_leaves_it_alone():
    """Only a failure is a later answer's to remove.  A quote arriving while
    the wallet is confirming must not wipe what that is saying.
    """
    page = swap_page_with(Wallet(), two_coins())
    page.view.say("Waiting for the transaction…", pending=True)

    page._quoted(None)

    assert page.view.status.visible
    assert page.view.status.text.value == "Waiting for the transaction…"


async def test_the_press_waits_for_the_quote_the_re_read_started():
    """`refresh` starts a quote and does not wait for it.  Planning before it
    lands reads the quote from *before* the re-read, and the new one arriving
    underneath drops the plan -- which is how a swap right after our own swap
    came back "the pools have moved" instead of a fresh quote.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Reverting()
    order: list[str] = []

    async def replan():
        order.append("plan")
        # The press prices afresh; that price is refused, and only the one
        # taken after the sweep goes through.
        page._plan = Reverting() if order.count("plan") == 1 else Planned()

    page._plan_now = replan
    page._read_balances = lambda: _answer(None)
    page._confirm = lambda *a, **k: _answer(0)
    page._page.run_task = lambda fn, *a: None

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def refresh(self):
            order.append("sweep")
            return 0

        async def settle(self):
            order.append("settle")

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert order == ["plan", "sweep", "settle", "plan"], "planned on the new quote"


async def test_a_press_with_no_plan_left_says_nothing_and_leaves_a_quote():
    """The re-read puts a fresh quote on screen, which is the answer to the
    press.  Telling somebody to press again on top of it is noise.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Reverting()

    async def replan():
        page._plan = None

    page._plan_now = replan
    sent = Sending()
    page._contract = lambda: sent

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def refresh(self):
            return 0

        async def settle(self):
            return None

    page.host = Host()

    await page._swap()

    assert not page.view.status.visible, "no spinner and nothing said"


def test_flipping_turns_the_balances_round_with_the_coins():
    """The balances are read from the wallet, which is a round trip.  Until it
    answered, each box showed the balance of the coin that used to be in it.
    """
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)
    page.view.show_balances(1_500_000, 250_000)
    before = (page.view.amount.hint_text, page.view.receive.hint_text)

    page.view._flip_clicked(None)

    assert page.view.pair == (usdt, usdc), "the coins turned round"
    assert page.view.amount.hint_text == before[1], "and so did the figures"
    assert page.view.receive.hint_text == before[0]


def test_flipping_onto_an_empty_balance_takes_the_max_button_away():
    """MAX is the sell box's, and after a flip the sell box holds what was
    being bought -- which may be a coin the wallet has none of.
    """
    page = swap_page_with(Wallet(), two_coins())
    page.view.show_balances(1_500_000, None)
    assert page.view.max_button.visible

    page.view._flip_clicked(None)

    assert not page.view.max_button.visible


async def test_both_balances_are_remembered_not_just_the_one_being_sold():
    """A flip makes the buy coin the sell coin at once, and MAX turns up with
    it -- with nothing to fill from until the next round trip answered.
    """
    coins = two_coins()
    usdc, usdt = coins
    page = swap_page_with(Wallet(), coins)

    class Holding:
        can_send = True

        async def balance_of(self, address):
            return 1_500_000 if address == usdc.address else 250_000

    holding = Holding()
    page._contract = lambda: holding

    await page._read_balances()

    assert page._balances[usdc.address] == 1_500_000
    assert page._balances[usdt.address] == 250_000, "the buy side too"


async def test_a_reverted_swap_still_says_so_after_the_re_read():
    """The recovery re-reads and re-quotes, and the quote landing underneath
    used to take the only explanation off the screen -- so a swap that
    reverted on chain said nothing at all.
    """
    page = swap_page_with(Wallet(), two_coins())
    page.view.say("The transaction was mined but reverted.", FAILED, sticky=True)

    page._quoted(None)

    assert page.view.status.visible, "an account of what happened stays put"
    assert "reverted" in page.view.status.text.value


async def test_a_refusal_to_plan_is_still_taken_down_by_a_quote():
    """Only what happened is sticky.  "This route would not go through" is a
    statement about a block that has since gone.
    """
    page = swap_page_with(Wallet(), two_coins())
    page.view.say("This route would not go through: <min", FAILED)

    page._quoted(None)

    assert not page.view.status.visible


async def test_the_tab_is_usable_again_before_the_housekeeping_finishes():
    """`after_swap` waits for the chain, sweeps every slot and re-solves.
    Held through that, the amount box stays disabled and MAX stale for tens of
    seconds after "Swapped." -- which reads as a frozen tab.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Planned()
    page._confirm = lambda *a, **k: _answer(0)
    page._read_balances = lambda: _answer(None)
    page._page.run_task = lambda fn, *a: None
    seen: list[bool] = []

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            seen.append(page.view.amount.disabled)
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert seen == [False], "the box was still disabled during the re-read"


async def test_a_reverted_send_re_reads_at_the_block_it_landed_in():
    """A reverted transaction was still mined.  Re-reading below that block
    re-reads the state the plan was already built against.
    """
    from curve.confirm import TransactionFailed

    page = swap_page_with(Wallet(), two_coins())
    page._read_balances = lambda: _answer(None)
    floors: list[int] = []

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            floors.append(not_before)
            return not_before

    page.host = Host()

    await page._after_failed_send(TransactionFailed("reverted", 4242).block)

    assert floors == [4242]


async def test_every_press_prices_the_route_again():
    """A plan is built when the typing stops; the press comes once somebody
    has read the numbers and decided, which is a minute later as often as not.
    Sending what was priced then is what makes a wallet say the transaction
    will fail before it is even signed.
    """
    page = swap_page_with(Wallet(), two_coins())
    priced: list[int] = []
    page._plan = Planned()          # already on screen, and already old

    async def replan():
        priced.append(1)
        page._plan = Planned()

    page._plan_now = replan
    page._read_balances = lambda: _answer(None)
    page._confirm = lambda *a, **k: _answer(0)
    page._page.run_task = lambda fn, *a: None

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert priced == [1], "it sent the plan on screen rather than a fresh one"
    assert sent.sent, "and it did go on to send"


class Batching(Sending):
    """A router contract whose wallet takes several calls in one prompt."""

    provider = object()
    account = "0x" + "11" * 20

    def __init__(self) -> None:
        super().__init__()
        self.approved: list = []

    def build_approve(self, plan):
        self.approved.append(plan)
        return ("0x" + "cc" * 20, "0xapprove")


async def test_an_approval_and_the_swap_go_over_together():
    """One prompt, not two -- which on a multisig is one round of cosigners
    rather than two.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._unapproved = True
    page._batches = True
    page.chain_id_now = 1
    plan = Planned()
    page._plan = plan
    page._plan_now = lambda: _answer(None)
    page._read_balances = lambda: _answer(None)
    page._page.run_task = lambda fn, *a: None
    sent: list = []

    async def send(_provider, _account, _chain, calls, **_kw):
        sent.append(calls)
        return "0xbatch"

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    contract = Batching()
    page._contract = lambda: contract

    import ui.swap_page as page_module
    original_send, original_wait = page_module.batch.send, None
    page_module.batch.send = send
    import curve.confirm as confirm_module
    original_wait = confirm_module.wait_for_batch
    confirm_module.wait_for_batch = lambda *a, **k: _answer(99)
    try:
        await page._swap()
    finally:
        page_module.batch.send = original_send
        confirm_module.wait_for_batch = original_wait

    assert len(sent) == 1, "the approval and the swap were not batched"
    assert len(sent[0]) == 2, "the batch did not carry both calls"
    assert not contract.sent, "it also sent the swap on its own"
    assert page._floor_block == 99, "the block the batch landed in was not kept"


async def test_an_outstanding_approval_is_batched_even_though_it_reverts():
    """With no allowance the dry run always reverts, on the `transferFrom`
    before any pool -- so a refusal says nothing about the route.  Holding
    back on one left the tab with nowhere to go: no approve button, because
    the wallet batches, and a swap reporting a revert it was always going to
    report.  That is what a Safe over WalletConnect showed.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._unapproved = True
    page._batches = True
    page.chain_id_now = 1
    sent: list = []

    async def send(_provider, _account, _chain, calls, **_kw):
        sent.append(calls)
        return "0xbatch"

    import curve.confirm as confirm_module
    import ui.swap_page as page_module
    original_send = page_module.batch.send
    original_wait = confirm_module.wait_for_batch
    page_module.batch.send = send
    confirm_module.wait_for_batch = lambda *a, **k: _answer(7)
    try:
        assert await page._send_as_batch(Batching(), Reverting()) is True
    finally:
        page_module.batch.send = original_send
        confirm_module.wait_for_batch = original_wait

    assert len(sent) == 1 and len(sent[0]) == 2


def test_a_batching_wallet_is_told_the_button_will_approve_too():
    """The approval step simply disappearing was worse than either spelling:
    the only way to learn one was still involved was to press Swap.
    """
    page = swap_page_with(Wallet(), two_coins())

    page.view.show_approval(True, batched=True)

    assert page.view.submit_button.content == "Approve & Swap"
    assert not page.view.approve_button.visible
    # The empty amount is what disables it here; the missing approval must not
    # also, or the one button offered would be one that cannot be pressed.
    assert page.view._unapproved is False


def test_a_wallet_without_batching_still_gets_the_two_steps():
    page = swap_page_with(Wallet(), two_coins())

    page.view.show_approval(True)

    assert page.view.submit_button.content == "2. Swap"
    assert page.view.approve_button.visible
    assert page.view.submit_button.disabled


async def test_a_wallet_that_does_not_batch_is_left_alone():
    page = swap_page_with(Wallet(), two_coins())
    page._unapproved = True
    page._batches = False

    assert await page._send_as_batch(Batching(), Planned()) is False


class Refusing(Sending):
    """A router whose chain says the call would revert, whatever we think."""

    def __init__(self, why: str = "leg below its minimum rate") -> None:
        super().__init__()
        self.why = why

    async def refused(self, _plan) -> str:
        return self.why


async def test_a_call_the_chain_refuses_is_not_offered():
    """The dry run runs in the app's local EVM, whose state is swept rather
    than live.  Where the two disagree the chain is right -- which is how a
    transaction the app was happy with reached a wallet that flagged it as
    certain to fail.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Planned()

    async def replan():
        page._plan = Planned()      # re-priced, and the chain still says no

    page._plan_now = replan
    page._restate = lambda: _answer(None)
    contract = Refusing()
    page._contract = lambda: contract

    class Host:
        pair = None
        session = None

        def request(self, _amount=0):
            pass

        async def refresh(self):
            return 0

        async def settle(self):
            return None

    page.host = Host()

    await page._swap()

    assert not contract.sent, "it sent a call the chain had already refused"
    assert "would not go through" in page.view.status.text.value
    assert "minimum rate" in page.view.status.text.value


async def test_the_chain_is_not_asked_while_an_approval_is_outstanding():
    """Without an allowance the call reverts on the `transferFrom` whatever
    the route would have done, so the answer would be about the approval."""
    page = swap_page_with(Wallet(), two_coins())
    page._unapproved = True

    assert await page._chain_refuses(Refusing(), Planned()) == ""


async def test_a_transport_that_cannot_answer_is_not_a_refusal():
    """Saying the chain refused, because nothing could be asked, would stop a
    route the chain never objected to."""
    page = swap_page_with(Wallet(), two_coins())

    class Unreachable(Sending):
        async def refused(self, _plan):
            raise RuntimeError("no endpoint")

    assert await page._chain_refuses(Unreachable(), Planned()) == ""


async def test_a_finished_swap_leaves_the_output_box_empty():
    """The host is still holding the amount, so the re-quote behind the
    refresh would put a figure straight back into the box the swap emptied --
    an answer for a trade that has already happened, sitting over the balance
    it changed.
    """
    page = swap_page_with(Wallet(), two_coins())
    page._plan = Planned()
    page._plan_now = lambda: _answer(None)
    page._confirm = lambda *a, **k: _answer(0)
    page._read_balances = lambda: _answer(None)
    page._page.run_task = lambda fn, *a: None
    page.view.receive.value = "876.5"
    wanted: list = []

    class Host:
        pair = None
        session = None

        def request(self, amount=0):
            wanted.append(amount)

        async def after_swap(self, not_before=0):
            return 0

    page.host = Host()
    sent = Sending()
    page._contract = lambda: sent

    await page._swap()

    assert wanted == [0], "the host kept the amount and will re-quote it"
    assert page.view.receive.value == ""


# -- the offer of source AGPL §13 asks for ----------------------------------


def _texts(control) -> list[str]:
    """Every string under a control, in order."""
    found: list[str] = []
    value = getattr(control, "value", None)
    if isinstance(value, str) and value:
        found.append(value)
    for name in ("content", "controls"):
        child = getattr(control, name, None)
        if child is None:
            continue
        for one in (child if isinstance(child, list) else [child]):
            found += _texts(one)
    return found


def _urls(control) -> list[str]:
    found: list[str] = []
    url = getattr(control, "url", None)
    if url is not None:
        found.append(getattr(url, "url", str(url)))
    for name in ("content", "controls"):
        child = getattr(control, name, None)
        if child is None:
            continue
        for one in (child if isinstance(child, list) else [child]):
            found += _urls(one)
    return found


def test_the_swap_page_offers_its_source():
    """§13 asks for the offer to reach whoever uses the program remotely, and
    a licence file in the repository does not reach them.  A build served off
    IPFS is exactly the case it is about.
    """
    view = build_view()

    words = " ".join(_texts(view._source))
    assert "AGPL-3.0" in words
    assert _urls(view._source) == [swap.SOURCE_URL, swap.ROUTER_URL]


def test_the_offer_names_the_router_it_bundles_as_well():
    """A build carries `src/erouter`, so the corresponding source is both.
    The app's repository pins the router as a submodule, so the first link
    reaches the second -- naming it saves anybody having to know that.
    """
    view = build_view()

    assert "github.com" in swap.ROUTER_URL
    assert swap.ROUTER_URL != swap.SOURCE_URL
    assert len(_urls(view._source)) == 2


def test_the_links_are_marked_as_links():
    """In the caption colour at caption size these read as a label until
    something says otherwise, and an offer nobody can see is not one."""
    view = build_view()

    underlined = [
        control for control in _walk(view._source)
        if getattr(getattr(control, "style", None), "decoration", None) is not None
    ]
    assert len(underlined) == 2, "the two links are not distinguishable"


def _walk(control):
    yield control
    for name in ("content", "controls"):
        child = getattr(control, name, None)
        if child is None:
            continue
        for one in (child if isinstance(child, list) else [child]):
            yield from _walk(one)


# -- a trade too small to price --------------------------------------------


class RoutingError(Exception):
    """The pipeline's, by the name `declined_for_size` matches on."""


FLOW = RoutingError(
    "flow conservation is violated by 2.496e-03 of the routed value at frxUSD "
    "(1 arc(s) in, 3 out) (achievable at this conditioning: 2.229e-04)"
)


def test_the_solver_declining_for_size_is_recognised() -> None:
    from router import declined_for_size

    assert declined_for_size(FLOW)
    assert not declined_for_size(RoutingError("src not connected to dst"))
    assert not declined_for_size(RuntimeError("flow conservation is violated"))


def _priced_page(amount: str):
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.set_prices({coins[0].address: 1.0})
    page.view.amount.value = amount
    return page


def test_a_sub_cent_trade_the_solver_declines_says_so_plainly() -> None:
    page = _priced_page("0.001")           # a tenth of a cent of USDC

    assert page._said(FLOW, 25_891_322) == "Swap size is too small to price."


def test_the_same_refusal_on_a_real_trade_keeps_the_real_message() -> None:
    """Naming the size there would be a guess, and a wrong one."""
    page = _priced_page("1000")

    said = page._said(FLOW, 25_891_322)
    assert said.startswith("flow conservation is violated")
    assert said.endswith("(block 25,891,322)")


def test_another_failure_at_a_tiny_size_is_still_reported() -> None:
    page = _priced_page("0.001")

    said = page._said(RoutingError("src not connected to dst"), 4242)
    assert said == "src not connected to dst (block 4,242)"


def test_no_price_to_judge_by_means_the_message_stands() -> None:
    """A coin the Prices API has nothing for: say what happened."""
    coins = two_coins()
    page = swap_page_with(Wallet(), coins)
    page.view.amount.value = "0.001"

    assert page.view.sell_worth_usd() is None
    assert page._said(FLOW, 0).startswith("flow conservation")
