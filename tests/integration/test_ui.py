"""End-to-end UI tests driven by Flet's own integration-testing framework."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.flet_ui

SETTLE_ATTEMPTS = 60

#: The last row of a full page, and the first of the page that is not there.
#: v2 caps a page at 50 rows, so a loaded list has `pool-row-49` and no
#: `pool-row-50`.
#:
#: These are keys rather than the "50 of 385 pools" caption the tests used to
#: read.  The caption is `count_label`, and `PoolList.set_layout` hides it in
#: the cards layout -- which is the layout the harness always gets, because
#: its window is narrow and `FletTestApp.resize_page` cannot help: it reads
#: `self.page`, and the page is not initialised when the app runs out of
#: process.  So the caption was never going to appear, and five tests waited
#: sixty pumps for it.  Keys say the same thing at any width.
LAST_ROW_OF_PAGE = "pool-row-49"
FIRST_ROW_PAST_PAGE = "pool-row-50"


async def wait_for(tester, finder_call, attempts: int = SETTLE_ATTEMPTS):
    """Pump until a finder matches something, then return it."""
    found = None
    for _ in range(attempts):
        await tester.pump_and_settle()
        found = await finder_call()
        if found.count:
            return found
    return found


async def wait_until_gone(tester, finder_call, attempts: int = SETTLE_ATTEMPTS):
    """Pump until a finder stops matching. Returns the final finder."""
    found = None
    for _ in range(attempts):
        await tester.pump_and_settle()
        found = await finder_call()
        if found.count == 0:
            return found
    return found


async def wait_for_pools(tester):
    """Pump until the list has rows in it."""
    return await wait_for(tester, lambda: tester.find_by_key("pool-row-0"))


async def wait_for_a_full_page(tester):
    return await wait_for(tester, lambda: tester.find_by_key(LAST_ROW_OF_PAGE))


async def test_app_starts_and_lists_pools(flet_app) -> None:
    tester = flet_app.tester
    await tester.pump_and_settle()

    assert (await tester.find_by_key("brand")).count == 1

    assert (await wait_for_pools(tester)).count == 1, "the list never got any rows"
    assert (await tester.find_by_key("pool-rows")).count == 1


async def test_the_list_opens_on_one_page_of_fifty(flet_app) -> None:
    """v2 caps a page at 50 rows, and the list asks for one page."""
    tester = flet_app.tester
    assert (await wait_for_a_full_page(tester)).count == 1, "the page never filled"
    assert (await tester.find_by_key(FIRST_ROW_PAST_PAGE)).count == 0, (
        "a 51st row means the list asked for more than one page")


async def test_the_list_offers_a_way_to_sort(flet_app) -> None:
    tester = flet_app.tester
    await wait_for_pools(tester)
    headings = (await tester.find_by_text("Volume")).count
    dropdown = (await tester.find_by_key("pool-sort")).count
    assert headings or dropdown, "no way to change the sort"


#: 3pool, at this address since 2020.  A *pool* address matches exactly one
#: pool where a coin address matches 713, which is what makes it a search that
#: narrows below a page: `PoolFeed.floor` sends no TVL floor with a search, and
#: "steth" -- which used to stand here -- now comes back with 84.
THREE_POOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"


async def test_searching_by_address_asks_the_server_and_narrows_the_list(
    flet_app,
) -> None:
    tester = flet_app.tester
    assert (await wait_for_a_full_page(tester)).count == 1

    search = await tester.find_by_key("pool-search")
    assert search.count == 1
    # Pasted in the case a block explorer shows it in, which the search is
    # expected to fold.
    await tester.enter_text(search, THREE_POOL)

    # A full page's worth of rows going away is the search having reached the
    # server and come back with fewer: nothing local narrows a page of fifty.
    remaining = await wait_until_gone(
        tester, lambda: tester.find_by_key(LAST_ROW_OF_PAGE)
    )
    assert remaining.count == 0, "the search never reached the server"

    assert (await wait_for_pools(tester)).count == 1, "it narrowed to nothing"
    assert (await tester.find_by_key("pool-row-1")).count == 0, (
        "a pool address should match exactly one pool"
    )


async def test_sorting_by_tvl_reloads_the_list(flet_app) -> None:
    """Whichever way this width offers: the column heading where there are
    headings, and the dropdown where the cards layout replaced them."""
    tester = flet_app.tester
    await wait_for_pools(tester)

    picker = await tester.find_by_key("pool-sort")
    if picker.count:
        await tester.tap(picker)
        await tester.pump_and_settle()
        # The option, not the closed dropdown's own label -- both read "TVL",
        # and the option is the one that appeared just now.
        option = await wait_for(tester, lambda: tester.find_by_text("TVL"))
        assert option.count >= 2, "the dropdown never opened"
        await tester.tap(option.last)
    else:
        heading = await tester.find_by_text("TVL")
        assert heading.count >= 1, "no way to sort by TVL at this width"
        await tester.tap(heading.first)

    assert (await wait_for_pools(tester)).count == 1, "the list never came back"


async def test_opening_a_pool_shows_the_action_panel(flet_app) -> None:
    tester = flet_app.tester
    await wait_for_pools(tester)

    row = await tester.find_by_key("pool-row-0")
    assert row.count == 1
    await tester.tap(row)
    await tester.pump_and_settle()

    for label in ("Deposit", "Withdraw", "Swap", "Stake"):
        found = await wait_for(tester, lambda label=label: tester.find_by_text(label))
        assert found.count >= 1, f"{label} tab missing from the detail page"

    assert (
        await wait_for(tester, lambda: tester.find_by_text_containing("LP token"))
    ).count >= 1
    assert (await tester.find_by_key("candle-size")).count == 1

    await wait_until_gone(tester, lambda: tester.find_by_text("Loading…"))

    await wait_until_gone(tester, lambda: tester.find_by_text("Loading…"))


async def test_the_chart_receives_candles(flet_app) -> None:
    """`set_candles` shows the chart and hides the empty note, so which of the
    two is on screen *is* whether the series arrived.

    Only where the chart is on screen at all.  The harness window is narrow,
    the chart sits below the fold, and Flutter does not build what it has not
    laid out -- and nothing here can scroll to it: the remote tester offers
    `tap`, `enter_text` and `mouse_hover`, and no drag.  So an absent chart
    *and* an absent empty note is this harness not reaching the thing, which
    is a skip; an empty note is the app saying it got nothing, which is a
    failure.

    It used to wait for a signed percentage, which is the depth chart's hover
    readout and is only drawn under the pointer.
    """
    tester = flet_app.tester
    await wait_for_pools(tester)
    await tester.tap(await tester.find_by_key("pool-row-0"))
    await wait_for(tester, lambda: tester.find_by_text_containing("LP token"))

    assert (await tester.find_by_key("candle-size")).count == 1, (
        "the pool opened without its chart pane")

    empty_note = "No price history for this pair."
    chart = await wait_for(
        tester, lambda: tester.find_by_key("price-chart"), attempts=15)
    if not chart.count:
        if (await tester.find_by_text(empty_note)).count:
            pytest.fail("the chart says there is no price history for this pair")
        pytest.skip("the chart is below this window's fold and nothing can scroll")

    assert (await tester.find_by_text(empty_note)).count == 0, (
        "the chart drew a series and kept its empty note up")
