"""Who gets to quote when.

The router's costs are paid at three different rates -- a warm per chain, a
preparation per pair, a solve per keystroke -- and the whole job of
`RouterHost` is to make that felt as one thing. These are the rules that make
it feel wrong when they are broken:

* a keystroke never waits for the keystroke before it, and never queues behind
  the ones it overtook;
* an amount typed *during* the warm is answered when the warm ends, not
  forgotten;
* an answer for a pair or a chain the reader has moved on from is dropped
  rather than drawn;
* an incomplete warm refuses rather than quoting against zeros.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from router.host import REFRESH_SECONDS, RETRY_SECONDS, RouterHost, Stage


class FakeReport:
    def __init__(self, unreadable=0):
        self.unreadable = unreadable
        self.complete = unreadable == 0
        self.warnings: list[str] = []


class FakeSession:
    """Enough of `RouterSession` to drive the host."""

    def __init__(self, *, unreadable=0, quote_ms=0.0):
        self.block = 100
        #: Scripted block heights for successive refreshes; empty means each
        #: one simply moves the chain on.
        self.blocks: list[int] = []
        self.solver = "rust"
        self.warms = 0
        self.pairs: list[tuple[str, str]] = []
        self.quoted: list[int] = []
        self.refreshes = 0
        self._unreadable = unreadable
        self._quote_ms = quote_ms
        self.gate: asyncio.Event | None = None

    async def warm(self, progress=None):
        self.warms += 1
        if self.gate is not None:
            await self.gate.wait()
        if progress:
            progress("storage", 0.5)
            progress("models", 1.0)
        return FakeReport(self._unreadable)

    async def set_pair(self, src, dst, progress=None):
        self.pairs.append((src, dst))
        return object()

    def quote(self, amount):
        self.quoted.append(amount)
        return f"quote:{amount}"

    async def refresh(self):
        self.refreshes += 1
        # `blocks` scripts what the endpoint answers, so a test can put a node
        # that has not moved in front of one that has -- which is what a load
        # balancer does, and what the real `refresh` turns into a no-op.
        self.block = self.blocks.pop(0) if self.blocks else self.block + 1
        return self.block


def build(session=None, **kw):
    session = session or FakeSession()
    seen: dict = {"quotes": [], "stages": [], "progress": [], "errors": []}

    async def make(chain_id):
        return session, [f"coin-on-{chain_id}"]

    host = RouterHost(
        make_session=kw.pop("make_session", make),
        on_stage=lambda stage, error: seen["stages"].append((stage, error)),
        on_progress=lambda phase, fraction: seen["progress"].append((phase, fraction)),
        on_quote=seen["quotes"].append,
        on_error=seen["errors"].append,
        **kw,
    )
    return host, session, seen


async def test_a_warm_chain_is_kept_and_a_second_visit_is_free():
    host, session, _seen = build()
    await host.open(1)
    assert host.stage is Stage.READY
    assert session.warms == 1
    assert host.coins == ["coin-on-1"]
    await host.open(1)
    assert session.warms == 1, "a chain already warmed is not warmed again"
    host.close()


async def test_an_incomplete_warm_refuses_rather_than_quoting():
    """An unread slot is a zero, and a zero fee is a plausible number."""
    host, _session, seen = build(FakeSession(unreadable=3))
    await host.open(1)
    assert host.stage is Stage.FAILED
    assert "3 slot(s)" in host.error
    host.request(5)
    await asyncio.sleep(0)
    assert seen["quotes"] == [], "a failed warm must not answer"
    host.close()


async def test_an_amount_typed_during_the_warm_is_answered_after_it():
    session = FakeSession()
    session.gate = asyncio.Event()
    host, _held, seen = build(session)
    warming = asyncio.ensure_future(host.open(1))
    await asyncio.sleep(0)
    host.request(42)                       # typed while the bar is moving
    assert seen["quotes"] == []
    session.gate.set()
    await warming
    await host.set_pair("0xa", "0xb")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [q.amount_in for q in seen["quotes"]] == [42]
    host.close()


async def test_only_the_newest_amount_is_answered():
    """Someone types faster than a solve; they want the answer for what is in
    the box, not one for every character they passed through."""
    host, session, seen = build()
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    for amount in (1, 12, 123, 1234):
        host.request(amount)
    for _ in range(8):
        await asyncio.sleep(0)
    assert [q.amount_in for q in seen["quotes"]] == [1234], seen["quotes"]
    assert session.quoted[-1] == 1234
    assert len(session.quoted) < 4, "the ones it overtook are not solved"
    host.close()


async def test_an_answer_for_a_pair_the_reader_left_is_dropped():
    """The pair changes while a solve is running: that answer is for a market
    nobody is looking at, and drawing it would show a number for the wrong
    pair with no way to tell."""
    session = FakeSession()
    host, _session, seen = build(session)
    await host.open(1)
    await host.set_pair("0xa", "0xb")

    moved: list[int] = []

    def quote(amount):
        # Stand in for the reader picking another coin mid-solve.
        if not moved:
            moved.append(amount)
            host._generation += 1
        session.quoted.append(amount)
        return f"quote:{amount}"

    session.quote = quote
    host.request(7)
    for _ in range(8):
        await asyncio.sleep(0)
    assert moved == [7], "the solve did run"
    assert seen["quotes"] == [], "and its answer was not drawn"
    host.close()


async def test_clearing_the_box_clears_the_answer():
    host, _session, seen = build()
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    host.request(0)
    assert seen["quotes"] == [None]
    host.close()


async def test_a_refresh_redoes_the_pair_and_the_showing_amount():
    """A refresh drops the preparation -- probes fitted at one block are a fit
    against a state that has moved."""
    host, session, seen = build()
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    host.request(9)
    for _ in range(6):
        await asyncio.sleep(0)
    seen["quotes"].clear()
    await host.refresh()
    for _ in range(6):
        await asyncio.sleep(0)
    assert session.refreshes == 1
    assert session.pairs == [("0xa", "0xb"), ("0xa", "0xb")], "the pair is redone"
    assert [q.amount_in for q in seen["quotes"]] == [9]
    host.close()


async def test_a_pair_a_refresh_could_not_redo_is_tried_again():
    """A refresh drops the preparation before redoing it. When the redo failed
    there was nothing left to redo *next* time -- `held.pair` was None, so the
    next refresh had no pair, `_pump` will not quote without one, and the tab
    quoted nothing until the coins were changed.
    """
    host, session, _seen = build()
    await host.open(1)
    await host.set_pair("0xa", "0xb")

    failed: list[int] = []
    ok = session.set_pair

    async def refuse(src, dst, progress=None):
        failed.append(1)
        raise RuntimeError("probes could not be fitted")

    session.set_pair = refuse
    await host.refresh()
    assert failed == [1]
    assert host.pair is None, "the preparation really was dropped"

    session.set_pair = ok
    await host.refresh()

    assert host.pair == ("0xa", "0xb"), "the pair came back on the next refresh"
    host.close()


class SlowRefresh(FakeSession):
    """A refresh whose sweep is out until `swept` is set, as on mainnet for 2-3 s."""

    def __init__(self) -> None:
        super().__init__()
        self.swept = asyncio.Event()
        self.sweeping = False
        self.quoted_mid_sweep: list[int] = []

    async def refresh(self):
        self.sweeping = True
        await self.swept.wait()
        self.sweeping = False
        return await super().refresh()

    def quote(self, amount):
        if self.sweeping:
            self.quoted_mid_sweep.append(amount)
        return super().quote(amount)


async def ready_with(session, pair=("0xa", "0xb"), **kw):
    host, _session, seen = build(session, **kw)
    await host.open(1)
    host._refresher.cancel()            # refreshes are driven by hand
    await host.set_pair(*pair)
    return host, seen


async def test_a_pair_picked_during_a_refresh_is_the_one_it_leaves_prepared():
    """The refresh took the pair before its sweep and re-prepared that after
    it: the widget showed the coins just picked, the router held the old ones,
    and every keystroke was answered for the old pair and dropped."""
    session = SlowRefresh()
    host, seen = await ready_with(session)
    refresh = asyncio.ensure_future(host.refresh())
    await asyncio.sleep(0)

    picking = asyncio.ensure_future(host.set_pair("0xc", "0xd"))
    for _ in range(4):
        await asyncio.sleep(0)
    assert not picking.done(), "the pick waits for the re-read"
    session.swept.set()
    await refresh
    assert await picking

    assert host.pair == ("0xc", "0xd")
    assert session.pairs[-1] == ("0xc", "0xd"), "and the session is prepared for it"
    host.request(5)
    for _ in range(6):
        await asyncio.sleep(0)
    assert [q.amount_in for q in seen["quotes"]] == [5]
    host.close()


async def test_a_keystroke_during_a_refresh_is_answered_after_it():
    """A solve during the sweep rebuilt the models from balances the sweep had
    not replaced yet, and the refresh's own rebuild then saw nothing to do."""
    session = SlowRefresh()
    host, seen = await ready_with(session)
    refresh = asyncio.ensure_future(host.refresh())
    await asyncio.sleep(0)

    host.request(7)
    for _ in range(6):
        await asyncio.sleep(0)
    assert seen["quotes"] == [], "parked while the state is being replaced"
    session.swept.set()
    await refresh
    await host.settle()

    assert session.quoted_mid_sweep == []
    assert [q.amount_in for q in seen["quotes"]] == [7]
    host.close()


async def test_a_pick_during_the_refreshs_own_re_preparation_is_not_undone():
    """Two preparations in flight finished in either order, and the session
    could end on the old pair while the host said the new one -- the one way
    a route for coins nobody chose could be drawn and sent."""
    session = FakeSession()
    host, _seen = await ready_with(session)
    slow = asyncio.Event()
    prepare = session.set_pair

    async def set_pair(src, dst, progress=None):
        if (src, dst) == ("0xa", "0xb"):
            await slow.wait()
        return await prepare(src, dst, progress)

    session.set_pair = set_pair
    refresh = asyncio.ensure_future(host.refresh())
    for _ in range(3):
        await asyncio.sleep(0)
    picking = asyncio.ensure_future(host.set_pair("0xc", "0xd"))
    await asyncio.sleep(0)
    slow.set()
    await refresh
    await picking

    assert host.pair == ("0xc", "0xd")
    assert session.pairs[-1] == ("0xc", "0xd")
    host.close()


async def test_a_background_refresh_does_not_raise_the_loading_bar():
    """Nothing ever lowered it: the bar sat full under the header from the
    first refresh on, and the tab looked busy for good."""
    host, seen = await ready_with(FakeSession())
    seen["progress"].clear()

    await host.refresh()

    assert seen["progress"] == []
    host.close()


async def test_a_refresh_that_fails_waits_before_trying_again():
    """Unstamped, a failed refresh was due again at once: one try and one red
    line a second, for as long as the endpoint was down."""
    now = [1000.0]

    async def sleep(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    session = FakeSession()
    tries: list[float] = []

    async def down():
        tries.append(now[0])
        raise RuntimeError("could not read the latest block header")

    host, _session, seen = build(session, sleep=sleep, clock=lambda: now[0])
    await host.open(1)
    session.refresh = down
    start = now[0]
    while now[0] - start < 600:
        await asyncio.sleep(0)

    assert len(tries) > 10
    assert {round(b - a, 3) for a, b in itertools.pairwise(tries)} == {RETRY_SECONDS}
    assert len(seen["errors"]) == len(tries)
    host.close()


async def test_state_left_by_a_sleeping_machine_is_re_read_before_it_is_quoted():
    """The monotonic clock stops while the machine sleeps, so after a night
    with the lid shut the state looked seconds old."""
    wall = [1_000_000.0]
    session = FakeSession()
    host, seen = await ready_with(session, wall=lambda: wall[0])
    before = session.refreshes

    wall[0] += 8 * 3600
    host.request(5)
    for _ in range(8):
        await asyncio.sleep(0)

    assert session.refreshes == before + 1
    assert [q.amount_in for q in seen["quotes"]] == [5], "answered after the re-read"
    host.close()


async def test_returning_to_a_chain_with_old_state_re_reads_it():
    """The timer only refreshes the chain on screen, so a chain left and come
    back to kept whatever it had when it was left."""
    now = [1000.0]
    session = FakeSession()
    host, _seen = await ready_with(session, clock=lambda: now[0])
    await host.open(2)
    before = session.refreshes

    now[0] += REFRESH_SECONDS + 1
    await host.open(1)
    for _ in range(4):
        await asyncio.sleep(0)

    assert session.refreshes == before + 1
    host.close()


async def test_a_swap_of_ours_forces_a_refresh():
    """Our own swap moved the pools it went through."""
    ticks: list[float] = []

    async def sleep(seconds):
        ticks.append(seconds)
        await asyncio.sleep(0)

    host, session, _seen = build(sleep=sleep)
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    before = session.refreshes
    await host.after_swap()
    assert session.refreshes == before + 1
    host.close()


async def test_the_refresher_sleeps_until_the_state_is_due():
    """Timestamped rather than ticking: a forced refresh resets the clock
    instead of racing it."""
    ticks: list[float] = []
    now = [1000.0]

    async def sleep(seconds):
        ticks.append(seconds)
        now[0] += seconds
        await asyncio.sleep(0)

    host, _session, _seen = build(sleep=sleep, clock=lambda: now[0])
    await host.open(1)
    for _ in range(4):
        await asyncio.sleep(0)
    assert ticks, "the refresher must be running"
    assert ticks[0] == pytest.approx(REFRESH_SECONDS, abs=1.0)
    host.close()


async def test_a_quote_that_raises_takes_the_old_figures_with_it():
    """The solver's tripwires fire on some sizes and not their neighbours, so
    a failed quote leaving the last good one on screen would put figures for a
    different trade under a red line about this one."""
    session = FakeSession()
    host, _session, seen = build(session)
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    host.request(100)
    for _ in range(6):
        await asyncio.sleep(0)
    assert [q.amount_in for q in seen["quotes"] if q] == [100]

    def boom(amount):
        raise RuntimeError("flow conservation is violated by 3.343e-03")

    session.quote = boom
    host.request(200)
    for _ in range(6):
        await asyncio.sleep(0)
    assert seen["quotes"][-1] is None, "the stale figures were cleared"
    assert seen["errors"], "and the reason was reported"
    host.close()


async def test_a_pair_that_would_not_prepare_is_not_quoted_anyway():
    """The one that got out.

    `held.pair` was cleared only on success, so a preparation that failed or
    was overtaken left the *previous* pair in place and every later keystroke
    was answered against it: the widget said crvUSD to sDOLA, the route said
    crvUSD to USDT, and the rate read "1 crvUSD = 0 sDOLA" because the old
    pair's numbers were being formatted with the new coin's decimals.
    """
    session = FakeSession()
    host, _session, seen = build(session)
    await host.open(1)
    assert await host.set_pair("0xa", "0xb")

    async def refuse(src, dst, progress=None):
        session.pairs.append((src, dst))
        raise RuntimeError("this pair cannot be priced")

    session.set_pair = refuse
    assert not await host.set_pair("0xa", "0xc")
    assert seen["errors"], "the refusal is reported"

    seen["quotes"].clear()
    host.request(5)
    for _ in range(8):
        await asyncio.sleep(0)
    assert seen["quotes"] == [], "and nothing is answered for the old pair"
    assert 5 not in session.quoted
    host.close()


async def test_a_pair_is_prepared_before_anything_is_quoted_against_it():
    session = FakeSession()
    host, _session, _seen = build(session)
    await host.open(1)
    host.request(7)
    for _ in range(4):
        await asyncio.sleep(0)
    assert session.quoted == [], "no pair yet, so nothing to quote"
    await host.set_pair("0xa", "0xb")
    for _ in range(6):
        await asyncio.sleep(0)
    assert session.quoted == [7]
    host.close()


async def test_a_failed_warm_is_reported_not_swallowed():
    async def make(chain_id):
        raise RuntimeError("the endpoint declined")

    host, _session, seen = build(make_session=make)
    await host.open(1)
    assert host.stage is Stage.FAILED
    assert "declined" in host.error
    assert seen["errors"], "the exception reaches the view"
    host.close()


async def test_settling_waits_for_the_quote_a_refresh_started():
    """`refresh` starts a quote and returns without it, which is right for a
    background re-read.  A caller about to act on the numbers wants the answer
    for the state just re-read, not the one from before it.
    """
    host, _session, seen = build()
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    host.request(9)
    for _ in range(6):
        await asyncio.sleep(0)
    seen["quotes"].clear()

    await host.refresh()
    assert not seen["quotes"], "the refresh did not wait, and should not have"

    await host.settle()

    assert [q.amount_in for q in seen["quotes"]] == [9], "the answer landed"
    host.close()


async def test_settling_with_nothing_in_flight_is_free():
    host, _session, _seen = build()
    await host.open(1)
    await host.settle()
    host.close()


async def test_a_swap_of_ours_is_not_re_read_from_a_node_behind_it():
    """The endpoint is a load balancer and its nodes sit at different heights.
    One still behind the block our swap landed in does not see it, and
    `refresh` does nothing at all when it answers with the block already held
    -- so the next quote is priced on pools this swap has already moved.
    """
    ticks: list[float] = []

    async def sleep(seconds):
        ticks.append(seconds)
        await asyncio.sleep(0)

    host, session, _seen = build(sleep=sleep)
    await host.open(1)
    await host.set_pair("0xa", "0xb")
    # Two nodes behind, then one that has caught up.
    session.blocks = [100, 100, 103]
    before = session.refreshes

    landed = await host.after_swap(103)

    assert landed >= 103, "it waited for the chain it had already seen"
    assert session.refreshes == before + 3, "and re-read once it had"
    assert ticks, "it paused between tries rather than spinning"
    host.close()


async def test_a_swap_with_no_block_to_wait_for_re_reads_once():
    host, session, _seen = build()
    await host.open(1)
    before = session.refreshes

    await host.after_swap()

    assert session.refreshes == before + 1
    host.close()
