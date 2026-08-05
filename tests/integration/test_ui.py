"""End-to-end UI tests driven by Flet's own integration-testing framework.

These are the real thing: `flet_app` starts the app and `tester` finds
controls, taps them and types into them. That covers layout, hit-testing and
paint -- exactly what the constructor tests in `tests/test_views.py` cannot
reach, and where every bug in this project so far has actually lived:

  * a `TextButton` column heading that hovered but never fired `on_click`;
  * `ft.Tab(content=…)`, which no longer exists in 0.86, blowing up only
    when a pool page was opened.

Marked `flet_ui` and excluded from the default run, because unlike
everything else here they need a Flutter test host. `flet-cli` downloads and
provisions one -- including the Flutter SDK itself, to `~/flutter/` -- so no
pre-installed SDK is required, but budget ~5 minutes for the first run
against ~25s per test warm:

    .venv/bin/python -m pytest tests/integration -m flet_ui

Two limits worth knowing before adding to this file:

  * In device mode (the default) `flet_app.tester` is a `RemoteTester`,
    whose API is a subset of `Tester`: find/tap/enter_text/screenshot are
    there, **`drag` is not**. So scrolling cannot be driven from here, and
    the scroll-to-load-a-page trigger is covered by
    `tests/test_views.py::test_scroll_near_the_end_is_what_triggers_a_page`
    instead.
  * `pump_and_settle` returns as soon as animations stop, which is long
    before a network call answers. Every wait below is therefore a polling
    loop, not a single settle.

The app reads the live Curve API, so a failure here can also mean the API is
unreachable rather than the UI being wrong.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.flet_ui

SETTLE_ATTEMPTS = 60

#: "50 of 385 pools" while paging, "14 pools" once everything matching is in.
COUNT_PATTERN = r"\d+ of \d+ pools|\d+ pools"
#: The list opens on exactly one page, because v2 caps a page at 50 rows.
FIRST_PAGE_PATTERN = r"^50 of \d+ pools$"


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
    return await wait_for(tester, lambda: tester.find_by_text_containing(COUNT_PATTERN))


async def test_app_starts_and_lists_pools(flet_app) -> None:
    """The list paints with real data from the API."""
    tester = flet_app.tester
    await tester.pump_and_settle()

    # The chrome renders before any data arrives.
    assert (await tester.find_by_text("CURVE")).count == 1

    assert (await wait_for_pools(tester)).count >= 1, "the pool count never appeared"
    assert (await tester.find_by_key("pool-row-0")).count == 1


async def test_the_list_opens_on_one_page_of_fifty(flet_app) -> None:
    """The paging contract, visible in the header: v2 caps a page at 50.

    Asserted on the header rather than by counting rows: `ListView`
    virtualises, so only the handful of rows currently on screen exist in
    the widget tree and `find_by_key("pool-row-40")` finds nothing even
    though the pool is loaded.
    """
    tester = flet_app.tester
    first = await wait_for(
        tester, lambda: tester.find_by_text_containing(FIRST_PAGE_PATTERN)
    )
    assert first.count == 1
    # The top row is built; a row far down the page is not, being off screen.
    assert (await tester.find_by_key("pool-row-0")).count == 1
    assert (await tester.find_by_key("pool-row-49")).count == 0


async def test_every_column_heading_renders(flet_app) -> None:
    tester = flet_app.tester
    await wait_for_pools(tester)
    for label in ("Pool", "Base APY", "Incentives", "Volume", "TVL"):
        assert (await tester.find_by_text(label)).count >= 1, label


async def test_searching_asks_the_server_and_narrows_the_list(flet_app) -> None:
    """Typing resets the feed and re-queries; it does not filter in memory.

    "steth" matches 14 pools above the TVL floor, so the header must stop
    saying "50 of …" -- a client-side filter over the 50 rows already
    loaded could never produce that.
    """
    tester = flet_app.tester
    assert (
        await wait_for(tester, lambda: tester.find_by_text_containing(FIRST_PAGE_PATTERN))
    ).count == 1

    search = await tester.find_by_key("pool-search")
    assert search.count == 1
    await tester.enter_text(search, "steth")

    remaining = await wait_until_gone(
        tester, lambda: tester.find_by_text_containing(FIRST_PAGE_PATTERN)
    )
    assert remaining.count == 0, "the search never reached the server"

    # A narrowed but non-empty list.
    assert (await wait_for_pools(tester)).count >= 1
    assert (await tester.find_by_key("pool-row-0")).count == 1


async def test_sorting_by_tvl_reloads_the_list(flet_app) -> None:
    """Tapping a heading resets the feed and reloads from page 1.

    The heading is a `Container` with `on_click` rather than a
    `TextButton`, because the latter hovered but never fired in a published
    build -- the kind of thing only a test at this level catches.
    """
    tester = flet_app.tester
    await wait_for_pools(tester)

    heading = await tester.find_by_text("TVL")
    assert heading.count >= 1
    await tester.tap(heading.first)

    assert (await wait_for_pools(tester)).count >= 1
    assert (await tester.find_by_key("pool-row-0")).count == 1


async def test_opening_a_pool_shows_the_action_panel(flet_app) -> None:
    """Tapping a row opens the detail page with all four action tabs.

    This is the path `ft.Tab(content=…)` broke: the list rendered perfectly
    and the app only died on the second click.
    """
    tester = flet_app.tester
    await wait_for_pools(tester)

    row = await tester.find_by_key("pool-row-0")
    assert row.count == 1
    await tester.tap(row)
    await tester.pump_and_settle()

    for label in ("Deposit", "Withdraw", "Swap", "Stake"):
        found = await wait_for(tester, lambda label=label: tester.find_by_text(label))
        assert found.count >= 1, f"{label} tab missing from the detail page"

    # The chart's series picker and its candle-size picker are both there.
    assert (
        await wait_for(tester, lambda: tester.find_by_text_containing("LP token"))
    ).count >= 1
    assert (await tester.find_by_key("candle-size")).count == 1

    # Let the chart request finish before the fixture tears the app down;
    # otherwise the Flutter process is killed mid-request and teardown errors.
    await wait_until_gone(tester, lambda: tester.find_by_text("Loading…"))


async def test_the_chart_receives_candles(flet_app) -> None:
    """The chart gets real data: the caption reports the window's change.

    That caption is only produced once at least two candles have arrived,
    so it is a proxy for "the chart has a series in it".

    What this does *not* prove is that the tooltip renders on hover.
    `find_by_key` does not reach inside a `flet-charts` control, and
    synthetic pointer events do not reach Flutter's hover hit-testing from
    Chrome DevTools either, so the interactivity is taken on the control's
    documented behaviour plus the unit test that every spot carries a
    tooltip string.
    """
    tester = flet_app.tester
    await wait_for_pools(tester)
    await tester.tap(await tester.find_by_key("pool-row-0"))

    await wait_for(tester, lambda: tester.find_by_text_containing("LP token"))
    caption = await wait_for(
        tester, lambda: tester.find_by_text_containing(r"[+-]\d+\.\d\d%")
    )
    assert caption.count >= 1, "the chart never reported a series"
