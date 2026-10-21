"""One warmed session per chain, and who gets to quote when.

The router's own shape is three costs, each paid at a different rate:

    warm       once per chain, and again per block for the state
    set_pair   when either token changes
    quote      per keystroke

A frontend has to make that felt as one thing.  What this owns is the timing:
the loading bar during a warm, a background re-read every two minutes and
after a swap of our own lands, and the rule that a keystroke never waits for
the keystroke before it -- only for a re-read, which it must not run inside.

Flet-free on purpose.  It reports progress and results through callbacks, so
the view decides what a stage looks like and this decides when one happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: How often the swept state is re-read while the tab is open.  Pool balances
#: move with every trade anyone makes, and a quote priced against a two-minute
#: old pool is a quote against a pool that has moved -- which the pre-submit
#: re-read catches, but only after someone has decided to sign it.
REFRESH_SECONDS = 120.0

#: How many times a re-read waits for the endpoint to reach a block we have
#: already seen a transaction confirmed in, and how long it waits between.
#: The router's own numbers, for the same load balancer and the same reason.
CATCH_UP_TRIES = 8
CATCH_UP_PAUSE = 1.5

#: How long a failed refresh waits before trying again.  The endpoint is a load
#: balancer and a raised read is usually a bad backend.
RETRY_SECONDS = 20.0


class Stage(Enum):
    """What the tab can say about a chain."""

    COLD = "cold"
    WARMING = "warming"
    READY = "ready"
    FAILED = "failed"


@dataclass(slots=True)
class Quote:
    """One answer, and everything the widget draws from it."""

    amount_in: int
    result: Any
    block: int
    solver: str
    elapsed_ms: float = 0.0


@dataclass(slots=True)
class _Chain:
    """What has been paid for on one chain."""

    session: Any = None
    stage: Stage = Stage.COLD
    error: str = ""
    pair: tuple[str, str] | None = None
    #: The pair last asked for, prepared or not: what a refresh redoes.
    asked: tuple[str, str] | None = None
    warmed_at: float = 0.0
    #: The same moment by the wall clock, which goes on while the machine sleeps.
    warmed_wall: float = 0.0
    #: Held across a refresh and every preparation, so a pick or a solve waits
    #: for the re-read instead of running against state it is replacing.
    busy: asyncio.Lock = field(default_factory=asyncio.Lock)
    coins: list = field(default_factory=list)


def declined_for_size(exc: BaseException) -> bool:
    """Is this the solver refusing a trade for want of something to fit?

    It says `flow conservation is violated by ... at <node>`, which is true
    and is not what the person typing wants told.  Below a certain size the
    probes the refine pass fits with land under the resolution the pools'
    own ladders already hold, and the circulation the solve is conditioned on
    is sized by the graph rather than by the trade -- so a small enough trade
    fails a gate that nothing is actually wrong with.  The router's
    `probe.MIN_OUT_QUANTA` is the same fact from the other side.

    By class *name* rather than by importing `RoutingError`: the pipeline is
    the submodule's, this module is imported whether or not it built, and the
    name is what a caller can check without reaching for either.
    """
    return (
        type(exc).__name__ == "RoutingError"
        and "flow conservation" in str(exc)
    )


class RouterHost:
    """The router, from the tab's point of view."""

    def __init__(self, *, make_session, on_stage=None, on_progress=None,
                 on_quote=None, on_error=None, sleep=None, clock=None, wall=None):
        #: `async (chain_id) -> (RouterSession, coins)`.  Injected so this can
        #: be driven by a fake in tests and by the real thing in the app.
        self._make_session = make_session
        self._on_stage = on_stage or (lambda *_: None)
        self._on_progress = on_progress or (lambda *_: None)
        self._on_quote = on_quote or (lambda *_: None)
        self._on_error = on_error or (lambda *_: None)
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or time.monotonic
        self._wall = wall or time.time

        self._chains: dict[int, _Chain] = {}
        self.chain_id = 0
        #: Bumped whenever the chain or the pair changes.  A quote that comes
        #: back under a stale generation is dropped rather than drawn -- the
        #: same guard `CurveApp.load_pools` uses, and for the same reason: every
        #: await is a place the reader can have moved on.
        self._generation = 0
        self._wanted: int | None = None
        self._quoting = False
        self._quoter: asyncio.Task | None = None
        self._refresher: asyncio.Task | None = None
        self._overdue: asyncio.Task | None = None

    # ------------------------------------------------------------- the chain

    @property
    def stage(self) -> Stage:
        return self._held.stage

    @property
    def session(self):
        return self._held.session

    @property
    def pair(self) -> tuple[str, str] | None:
        """The pair actually prepared, lowercased.  `None` while there is none."""
        return self._held.pair

    @property
    def coins(self) -> list:
        return self._held.coins

    @property
    def error(self) -> str:
        return self._held.error

    @property
    def _held(self) -> _Chain:
        return self._chains.setdefault(self.chain_id, _Chain())

    async def open(self, chain_id: int) -> None:
        """Show this chain, warming it if this is the first time.

        A chain already warmed keeps its session: switching away and back is
        free, which is what makes the network picker usable at all.
        """
        self.chain_id = int(chain_id)
        self._generation += 1
        held = self._held
        if held.stage is Stage.READY:
            self._say(held.stage)
            self._refresh_if_overdue()
            return
        if held.stage is Stage.WARMING:
            return
        await self._warm()

    async def _warm(self) -> None:
        held = self._held
        generation = self._generation
        held.stage = Stage.WARMING
        held.error = ""
        self._say(Stage.WARMING)
        try:
            session, coins = await self._make_session(self.chain_id)
            report = await session.warm(self._progress)
        except Exception as exc:
            if generation != self._generation:
                return
            held.stage = Stage.FAILED
            held.error = str(exc)
            self._say(Stage.FAILED)
            self._on_error(exc)
            return
        if generation != self._generation:
            return          # the reader moved on; this warm is somebody else's
        if not report.complete:
            # An unread slot is a zero, and a zero fee or rate is a plausible
            # number: the quote succeeds and is wrong.  Refuse rather than
            # answer -- see `LocalEvm`.
            held.stage = Stage.FAILED
            held.error = (
                f"{report.unreadable} slot(s) could not be read, so a quote "
                f"here would be priced against zeros"
            )
            self._say(Stage.FAILED)
            return
        held.session = session
        held.coins = coins
        self._stamp(held)
        held.stage = Stage.READY
        held.pair = None
        self._say(Stage.READY)
        self._start_refresher()
        # Someone who typed an amount while the bar was moving is owed an
        # answer now rather than another keystroke.
        if self._wanted is not None:
            self._pump()

    # -------------------------------------------------------------- the pair

    async def set_pair(self, src: str, dst: str) -> bool:
        """Probe and price this pair.  False if the chain is not ready."""
        held = self._held
        if held.stage is not Stage.READY or held.session is None:
            return False
        pair = (src.lower(), dst.lower())
        held.asked = pair
        if held.pair == pair:
            return True
        self._generation += 1
        generation = self._generation
        # Forgotten *before* the new one is prepared, not after.  Kept until
        # then, a preparation that failed or was overtaken left the old pair
        # in place and every later keystroke was answered against it -- the
        # widget said crvUSD to sDOLA, the route said crvUSD to USDT, and the
        # rate read "1 crvUSD = 0 sDOLA" because the old pair's numbers were
        # being formatted with the new coin's decimals.
        held.pair = None
        async with held.busy:
            if held.pair == pair:
                ready = True        # a refresh prepared it while this waited
            elif generation != self._generation:
                return False        # overtaken while it waited
            else:
                ready = await self._prepare(held, pair, generation)
        if ready:
            self._pump()
        return ready

    async def _prepare(self, held: _Chain, pair: tuple[str, str], generation: int,
                       *, quiet: bool = False) -> bool:
        """Prepare `pair` under `held.busy`.  `quiet` draws nothing for it."""
        try:
            await held.session.set_pair(
                *pair, progress=None if quiet else self._pair_progress)
        except Exception as exc:
            if generation == self._generation:
                self._on_error(exc)
            return False
        if generation != self._generation:
            return False
        held.pair = pair
        if not quiet:
            self._on_progress("ready", 1.0)
        return True

    # ------------------------------------------------------------ the amount

    def request(self, amount_in: int) -> None:
        """Quote this amount, as soon as there is nothing else to answer.

        Latest wins, and nothing queues: a quote takes ~300 ms and someone
        types faster than that, so the answer they want is the one for what is
        in the box now -- not one for each character they passed through.
        There is no debounce either, because a debounce makes the first answer
        arrive later for no benefit when the input has stopped changing.
        """
        self._wanted = int(amount_in) if amount_in else None
        if self._wanted is None:
            self._on_quote(None)
            return
        self._refresh_if_overdue()
        self._pump()

    def _pump(self) -> None:
        if self._quoting or self._wanted is None:
            return
        if not self._can_quote(self._held):
            return          # parked until the warm, the pair or the re-read is done
        self._quoting = True
        # Held, so it is not collected mid-flight -- asyncio keeps only a weak
        # reference to a running task.
        self._quoter = asyncio.ensure_future(self._quote_loop())

    async def _quote_loop(self) -> None:
        """Answer the newest amount until nothing new has arrived."""
        try:
            while True:
                # The solve is synchronous and ~300 ms; yield first so the
                # keystroke that asked for it has been drawn.  Everything is
                # read after the yield: a pair or a re-read can land during it.
                await self._sleep(0)
                held = self._held
                amount = self._wanted
                if amount is None or not self._can_quote(held):
                    return
                generation = self._generation
                started = self._clock()
                try:
                    result = held.session.quote(amount)
                except Exception as exc:
                    if generation == self._generation and amount == self._wanted:
                        # The figures on screen are for an amount that is no
                        # longer in the box, so they go before the reason for
                        # their going does.  Left up, they would sit under a
                        # red line describing a *different* trade, with
                        # nothing anywhere saying the two disagree -- the
                        # solver's own tripwires fire on some sizes and not
                        # their neighbours, so this is not a rare shape.
                        self._on_quote(None)
                        self._on_error(exc)
                        self._wanted = None
                    return
                if generation != self._generation:
                    return
                if amount == self._wanted:
                    self._on_quote(Quote(
                        amount_in=amount,
                        result=result,
                        block=held.session.block,
                        solver=getattr(held.session, "solver", ""),
                        elapsed_ms=(self._clock() - started) * 1000,
                    ))
                    return
                # It changed while this one ran: answer the new one instead.
        finally:
            self._quoting = False

    @staticmethod
    def _can_quote(held: _Chain) -> bool:
        return (held.stage is Stage.READY and held.session is not None
                and held.pair is not None and not held.busy.locked())

    # ------------------------------------------------------------- refreshing

    def _start_refresher(self) -> None:
        if self._refresher is None or self._refresher.done():
            self._refresher = asyncio.ensure_future(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        """Re-read the swept state on a timer, with nothing drawn for it.

        Timestamped, the way `CurveApp.refresh_totals` does it: the loop sleeps
        until the state is actually due, so a refresh forced by a confirmed
        swap resets the clock instead of racing it.
        """
        while True:
            held = self._held
            await self._sleep(max(REFRESH_SECONDS - self._age(held), 1.0))
            held = self._held
            if held.stage is not Stage.READY or held.session is None:
                continue
            if not self._due(held):
                continue
            await self.refresh(only_if_due=True)

    def _refresh_if_overdue(self) -> None:
        """Re-read now, not at the next tick, when the state is past due.

        The tick sleeps on a clock that stops while the machine does, and only
        ever refreshes the chain that was on screen when it went to sleep.
        """
        held = self._held
        if held.stage is not Stage.READY or held.busy.locked():
            return
        if not self._due(held):
            return
        if self._overdue is None or self._overdue.done():
            self._overdue = asyncio.ensure_future(self.refresh(only_if_due=True))

    def _age(self, held: _Chain) -> float:
        """Seconds since the last read, by whichever clock says more."""
        if not held.warmed_at:
            return REFRESH_SECONDS
        return max(self._clock() - held.warmed_at, self._wall() - held.warmed_wall)

    def _due(self, held: _Chain) -> bool:
        # Within the loop's one-second floor counts: the two clocks never agree
        # to the microsecond, and short of due by one it slept a second more.
        return self._age(held) > REFRESH_SECONDS - 1.0

    def _stamp(self, held: _Chain, ago: float = 0.0) -> None:
        held.warmed_at = self._clock() - ago
        held.warmed_wall = self._wall() - ago

    async def refresh(self, *, only_if_due: bool = False) -> int:
        """Re-read the state at the newest block, and re-quote if anything is
        showing.  Nothing is drawn for this: it is not a wait anyone asked for.
        """
        held = self._held
        if held.stage is not Stage.READY or held.session is None:
            return 0
        async with held.busy:
            if only_if_due and not self._due(held):
                return 0        # done while this waited
            try:
                block = await held.session.refresh()
            except Exception as exc:
                # Due again in RETRY_SECONDS.  Unstamped, it was due again at
                # once, and the loop retried every second for as long as the
                # endpoint was down.
                self._stamp(held, ago=REFRESH_SECONDS - RETRY_SECONDS)
                self._on_error(exc)
                return 0
            self._stamp(held)
            # A refresh drops the preparation, because probes fitted at one
            # block are a fit against a state that has moved.  Redo it now
            # rather than on the next keystroke, which is where it would be
            # felt -- and redo the pair asked for last, which a pick made
            # during the re-read has changed.
            held.pair = None
            if held.asked is not None:
                await self._prepare(held, held.asked, self._generation, quiet=True)
        self._pump()
        return block

    async def settle(self) -> None:
        """Wait for the quote already in flight, if there is one.

        `refresh` starts one and does not wait -- nothing on screen is waiting
        for it either, which is the right answer for a background re-read.  A
        caller about to act on the numbers is the exception: it wants the
        answer for the state that was just re-read, not the one from before.
        """
        quoter = self._quoter
        if quoter is not None and not quoter.done():
            with contextlib.suppress(Exception):
                await quoter

    async def after_swap(self, not_before: int = 0) -> int:
        """A swap of ours landed, so the pools it went through have moved.

        `not_before` is the block it landed in.  The endpoint is a load
        balancer and its nodes are not at the same height, so a re-read taken
        from one still behind does not see our own swap -- and `refresh` does
        nothing at all when that node answers with the block already held.
        The next quote is then priced on pools this swap has already moved,
        while `plan_call` waits for the block and dry-runs against the ones it
        did.  A `min_out` taken off that quote is a promise the chain will not
        keep, and the call comes back "below min_out".

        So the re-read waits for the chain the caller has already seen, which
        is what `plan_call` does with the same number.
        """
        held = self._held
        held.warmed_at = 0.0
        block = await self.refresh()
        for _ in range(CATCH_UP_TRIES):
            if not not_before or block >= not_before:
                break
            await self._sleep(CATCH_UP_PAUSE)
            held.warmed_at = 0.0
            block = await self.refresh()
        return block

    def close(self) -> None:
        for task in (self._refresher, self._quoter, self._overdue):
            if task is not None:
                task.cancel()
        self._refresher = self._quoter = self._overdue = None

    # ------------------------------------------------------------- reporting

    def _say(self, stage: Stage) -> None:
        self._on_stage(stage, self._held.error)

    def _progress(self, phase: str, fraction: float) -> None:
        self._on_progress(phase, fraction)

    def _pair_progress(self, phase: str, fraction: float) -> None:
        """A pair's own 0..1, not the warm's phase weighting.

        The session reports both through one callback, and the warm's weights
        map an unknown phase to 1.0 -- so a pair jumped the bar straight to
        full and back.
        """
        self._on_progress("pair", fraction)
