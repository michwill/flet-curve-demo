"""The voting escrow and the distributor beside it."""

from __future__ import annotations

import datetime as dt

import pytest

from curve import abi
from curve.vecrv import (
    CRV,
    FEE_DISTRIBUTOR,
    MAXTIME,
    VOTING_ESCROW,
    WEEK,
    Lock,
    VeCrvContract,
    VeCrvError,
    week_floor,
)
from wallet.base import RpcError, WalletProvider

ACCOUNT = "0x1111111111111111111111111111111111111111"


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


class FakeProvider(WalletProvider):
    """Answers `eth_call` by selector, and records what it was sent."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[dict] = []
        self.sent: list[dict] = []
        #: Multicall3 is off unless a test says otherwise, so the fallback
        #: loop is what these exercise by default.
        self.aggregates = False

    async def request(self, method: str, params=None):
        params = params or []
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_call":
            self.calls.append(params[0])
            data = params[0].get("data", "")
            if params[0].get("to", "").lower().startswith("0xca11bde"):
                if not self.aggregates:
                    raise RpcError(-32000, "no multicall here")
                return self._aggregate(data)
            return self.answers.get(data[:10], word(0))
        if method == "eth_sendTransaction":
            self.sent.append(params[0])
            return "0x" + f"{len(self.sent):02x}" * 32
        raise AssertionError(f"unexpected method {method}")

    def _aggregate(self, _data: str) -> str:
        raise AssertionError("this fake does not encode aggregate3 answers")


def contract(**answers) -> tuple[VeCrvContract, FakeProvider]:
    provider = FakeProvider(answers)
    return VeCrvContract(provider, ACCOUNT), provider


# -- the week the escrow rounds to -----------------------------------------


def test_an_unlock_time_is_floored_to_the_escrows_own_week() -> None:
    """It rounds down to a Thursday; so does this, or the date on screen and
    the date on chain differ by up to a week."""
    when = int(dt.datetime(2026, 9, 4, 13, 30, tzinfo=dt.UTC).timestamp())

    floored = week_floor(when)

    assert floored <= when
    assert floored % WEEK == 0
    assert dt.datetime.fromtimestamp(floored, dt.UTC).strftime("%a") == "Thu"


def test_a_time_already_on_the_boundary_is_left_alone() -> None:
    on_it = week_floor(1_800_000_000)
    assert week_floor(on_it) == on_it


def test_the_calls_that_take_a_date_floor_it_too() -> None:
    """Not the caller's job to remember: both are built here."""
    c, _ = contract()
    when = 1_800_000_123

    _, create = c.build_create_lock(10, when)
    _, extend = c.build_increase_unlock_time(when)

    assert create.endswith(f"{week_floor(when):064x}")
    assert extend.endswith(f"{week_floor(when):064x}")


# -- what a lock is --------------------------------------------------------


def test_a_lock_that_has_run_out_can_be_withdrawn_and_not_added_to() -> None:
    ended = Lock(amount=10**18, end=1_000)

    assert ended.exists
    assert ended.expired(1_001)
    assert ended.seconds_left(1_001) == 0


def test_a_lock_still_running_is_not_expired() -> None:
    running = Lock(amount=10**18, end=2_000)

    assert not running.expired(1_000)
    assert running.seconds_left(1_000) == 1_000


def test_no_lock_at_all_is_never_expired() -> None:
    """Nothing to withdraw is not the same as something that has ended."""
    assert not Lock().exists
    assert not Lock().expired(10**12)


def test_the_amount_is_read_as_the_signed_word_it_is() -> None:
    """`locked` answers an `int128`, and the app reads two words."""
    amount, end = abi.decode_locked(word(5 * 10**18) + f"{1_800_000_000:064x}")

    assert (amount, end) == (5 * 10**18, 1_800_000_000)


# -- the approval that must never be unlimited -----------------------------


def test_the_escrow_is_approved_exactly_and_has_no_unlimited_path() -> None:
    """`deposit_for` is public, so an infinite allowance on the escrow is one
    anybody may spend on your behalf -- on the lock you already have."""
    c, _ = contract()

    to, data = c.build_approve(1234 * 10**18)

    assert to == CRV
    assert data.endswith(f"{1234 * 10**18:064x}")
    assert abi.MAX_UINT256 not in (int(data[-64:], 16),)


def test_an_approval_with_no_amount_is_refused_rather_than_widened() -> None:
    c, _ = contract()

    with pytest.raises(VeCrvError):
        c.build_approve(0)


# -- the reads -------------------------------------------------------------


async def test_the_lock_is_read_off_the_escrow() -> None:
    c, provider = contract(**{
        "0x" + abi.selector("locked(address)"):
            word(7 * 10**18) + f"{1_800_000_000:064x}",
    })

    lock = await c.locked()

    assert (lock.amount, lock.end) == (7 * 10**18, 1_800_000_000)
    assert provider.calls[0]["to"] == VOTING_ESCROW


async def test_the_claimable_amount_is_the_send_run_as_a_call() -> None:
    """`claim` is not a `view` and answers what it moved, so the preview and
    the transaction cannot disagree about the number."""
    c, provider = contract(**{"0x" + abi.selector("claim(address)"): word(42 * 10**18)})

    assert await c.claimable() == 42 * 10**18
    assert provider.calls[0]["to"] == FEE_DISTRIBUTOR
    assert provider.sent == [], "a preview sends nothing"


async def test_a_read_that_fails_says_which_one() -> None:
    class Broken(FakeProvider):
        async def request(self, method, params=None):
            if method == "eth_call":
                raise RpcError(-32000, "execution reverted")
            return await super().request(method, params)

    c = VeCrvContract(Broken(), ACCOUNT)

    with pytest.raises(VeCrvError, match="the lock"):
        await c.locked()


async def test_a_snapshot_asks_for_everything_the_page_draws() -> None:
    c, _ = contract(**{
        "0x" + abi.selector("locked(address)"):
            word(7 * 10**18) + f"{1_800_000_000:064x}",
        "0x" + abi.selector("balanceOf(address)"): word(3 * 10**18),
        "0x" + abi.selector("totalSupply()"): word(30 * 10**18),
        "0x" + abi.selector("allowance(address,address)"): word(5),
        "0x" + abi.selector("claim(address)"): word(11 * 10**18),
    })

    got = await c.snapshot()

    assert got.lock.amount == 7 * 10**18
    assert got.voting_power == 3 * 10**18
    assert got.total_voting_power == 30 * 10**18
    assert got.allowance == 5
    assert got.claimable == 11 * 10**18
    assert got.share == pytest.approx(10.0)


async def test_a_snapshot_falls_back_where_there_is_no_aggregator() -> None:
    """The fake refuses Multicall3, which is the path this takes."""
    c, provider = contract()

    await c.snapshot()

    assert any(call["to"].lower().startswith("0xca11bde") for call in provider.calls)
    assert len(provider.calls) == 7, "the aggregate, then one call each"


async def test_no_voting_power_anywhere_is_a_share_of_nothing() -> None:
    c, _ = contract()

    got = await c.snapshot()

    assert got.share == 0.0


# -- the writes ------------------------------------------------------------


async def test_creating_a_lock_goes_to_the_escrow() -> None:
    c, provider = contract()

    await c.create_lock(10**18, 1_800_000_123)

    assert provider.sent[0]["to"] == VOTING_ESCROW
    assert provider.sent[0]["value"] == "0x0"


async def test_withdrawing_takes_no_argument() -> None:
    c, provider = contract()

    await c.withdraw()

    assert provider.sent[0]["data"] == "0x" + abi.selector("withdraw()")


async def test_claiming_names_the_address_it_pays() -> None:
    c, provider = contract()

    await c.claim()

    assert provider.sent[0]["to"] == FEE_DISTRIBUTOR
    assert provider.sent[0]["data"].endswith(ACCOUNT[2:].lower().rjust(64, "0"))


async def test_a_wallet_that_cannot_send_is_told_so_rather_than_asked() -> None:
    from wallet.base import WalletError

    c = VeCrvContract(FakeProvider(), "")

    with pytest.raises(WalletError):
        await c.claim()


async def test_collecting_records_the_calls_instead_of_sending_them() -> None:
    """A wallet that batches wants the calls, not four prompts."""
    c, provider = contract()

    with c.collecting() as calls:
        await c.approve(10**18)
        await c.create_lock(10**18, 1_800_000_123)

    assert provider.sent == []
    assert [call.to for call in calls] == [CRV, VOTING_ESCROW]


def test_four_years_is_what_the_page_offers() -> None:
    """The contract stays the authority -- `MAXTIME()` is not a getter on
    this deployment, so there is nothing to read it from."""
    assert MAXTIME == 4 * 365 * 24 * 60 * 60


async def test_no_wallet_is_an_empty_answer_and_not_a_traceback() -> None:
    """Reading for the empty address throws out of `_address` before any of
    it is sent, and a page with no wallet wants an empty answer."""
    c = VeCrvContract(FakeProvider(), "")

    got = await c.snapshot()

    assert (got.lock.amount, got.claimable, got.share) == (0, 0, 0.0)
    assert not got.lock.exists


# -- what the panel offers, and when ---------------------------------------


#: A fixed clock, so "expired" and "still running" are decided by the test
#: rather than by when it is run.
NOW = 1_800_000_000.0


def view(**snapshot_kw):
    """`VeCrvView` with that clock and a snapshot already shown."""
    from curve.vecrv import Snapshot
    from ui.vecrv import VeCrvView

    class StubPage:
        def update(self) -> None: ...
        def run_task(self, *_a, **_k) -> None: ...

    v = VeCrvView(StubPage(), contract_for=lambda: None, now=lambda: NOW)
    v.show(Snapshot(**{"lock": Lock(), **snapshot_kw}))
    return v


def test_with_no_lock_the_panel_offers_to_make_one() -> None:
    v = view(crv=10**18)

    assert v.lock_title.value == "Lock CRV"
    assert v.lock_button.content == "Create lock"
    assert v.lock_button.visible
    assert not v.extend_button.visible
    assert not v.withdraw_button.visible


def test_with_a_lock_running_it_offers_to_add_and_to_extend() -> None:
    v = view(lock=Lock(amount=10**18, end=int(NOW) + 90 * 86400))

    assert v.lock_title.value == "Add to your lock"
    assert v.lock_button.content == "Add CRV"
    assert v.extend_button.visible


def test_an_expired_lock_offers_only_to_take_it_back() -> None:
    """The escrow refuses both `increase_amount` and `increase_unlock_time`
    once the end has passed, so offering either would offer a revert."""
    v = view(lock=Lock(amount=5 * 10**18, end=int(NOW) - 1))

    assert v.lock_title.value == "Withdraw"
    assert v.withdraw_button.visible
    assert "5" in v.withdraw_button.content
    assert not v.lock_button.visible
    assert not v.extend_button.visible
    assert not v.amount.visible and not v.date.visible


def test_claiming_is_offered_only_when_there_is_something_to_claim() -> None:
    empty = view()
    paid = view(claimable=3 * 10**18)

    assert empty.claim_button.disabled
    assert not paid.claim_button.disabled
    assert "3" in paid.claimable.value


def test_the_amount_has_to_be_approved_before_it_can_be_locked() -> None:
    v = view(crv=10 * 10**18, allowance=0)
    v.amount.value = "2"
    v._sync()

    assert v.approve_button.visible
    assert v.approve_button.content == "Approve 2 CRV"
    assert v.lock_button.disabled, "not approved yet"


def test_and_once_it_is_the_approval_goes_away() -> None:
    v = view(crv=10 * 10**18, allowance=2 * 10**18)
    v.amount.value = "2"
    v.date.value = "2030-08-29"
    v._sync()

    assert not v.approve_button.visible
    assert not v.lock_button.disabled


def test_more_than_the_wallet_holds_is_not_offered() -> None:
    v = view(crv=10**18, allowance=10**30)
    v.amount.value = "5"
    v.date.value = "2030-08-29"
    v._sync()

    assert v.lock_button.disabled


def test_extending_needs_a_date_later_than_the_one_there_already() -> None:
    end = int(NOW) + 365 * 86400
    v = view(lock=Lock(amount=10**18, end=end))

    v.date.value = "2027-01-07"          # sooner than the lock already runs
    v._sync()
    assert v.extend_button.disabled

    v.date.value = "2030-08-29"
    v._sync()
    assert not v.extend_button.disabled


def test_the_estimate_is_the_escrows_own_arithmetic() -> None:
    from ui.vecrv import MAXTIME, voting_power_for

    assert voting_power_for(10**18, MAXTIME) == 10**18
    assert voting_power_for(10**18, MAXTIME // 4) == 10**18 // 4
    assert voting_power_for(10**18, 0) == 0
    assert voting_power_for(0, MAXTIME) == 0
    assert voting_power_for(10**18, MAXTIME * 2) == 10**18, "capped at four years"


def test_a_lock_that_runs_longer_than_a_preset_disables_it() -> None:
    """A lock only ever moves outwards: with three years left, "1y" names a
    date the escrow would refuse, so the button is dead rather than there to
    be pressed and told no."""
    from ui.vecrv import MAXTIME, WEEK

    three_years = int(NOW) + 3 * 365 * 86400
    v = view(lock=Lock(amount=10**18, end=three_years))

    assert not v.preset_reachable(WEEK)
    assert not v.preset_reachable(52 * WEEK)
    assert v.preset_reachable(MAXTIME), "four years is still further out"
    assert v._preset_buttons[WEEK].disabled
    assert not v._preset_buttons[MAXTIME].disabled


def test_with_no_lock_every_preset_is_available() -> None:
    from ui.vecrv import MAXTIME, WEEK

    v = view()

    assert all(v.preset_reachable(s) for s in (WEEK, 4 * WEEK, 52 * WEEK, MAXTIME))
    assert not any(b.disabled for b in v._preset_buttons.values())


def test_a_lock_already_at_the_maximum_can_be_extended_by_nothing() -> None:
    from ui.vecrv import MAXTIME

    v = view(lock=Lock(amount=10**18, end=int(NOW) + MAXTIME))

    assert all(b.disabled for b in v._preset_buttons.values())


def test_a_preset_names_a_date_measured_from_now() -> None:
    """Not from the end already there: the button says "1y", and a lock that
    ends in a year is what that means whether or not one exists."""
    from curve.vecrv import week_floor
    from ui.vecrv import WEEK

    fresh = view()
    held = view(lock=Lock(amount=10**18, end=int(NOW) + 30 * 86400))

    assert fresh.preset_date(52 * WEEK) == week_floor(int(NOW) + 52 * WEEK)
    assert held.preset_date(52 * WEEK) == fresh.preset_date(52 * WEEK)


def test_and_never_past_the_four_years_the_escrow_allows() -> None:
    from ui.vecrv import MAXTIME

    v = view()

    assert v.preset_date(MAXTIME * 2) <= int(NOW) + MAXTIME


def test_pressing_an_unreachable_preset_does_nothing() -> None:
    from ui.vecrv import WEEK

    v = view(lock=Lock(amount=10**18, end=int(NOW) + 3 * 365 * 86400))

    v._preset_clicked(WEEK)

    assert not v.date.value


def test_the_note_is_as_wide_as_the_two_panels_it_sits_over() -> None:
    """Its edges are the point: they have to land on the panels' edges."""
    from ui.responsive import layout_for
    from ui.vecrv import PANEL_WIDTH, SPAN

    v = view()
    v.set_layout(layout_for(1400.0))

    assert SPAN == PANEL_WIDTH * 2 + 16
    assert v.position.width == v.band.width == SPAN


def test_and_as_wide_as_the_one_panel_once_they_stop_fitting_side_by_side()\
        -> None:
    """The row wraps rather than squeezes, so under it there is one panel."""
    from ui.responsive import BODY_PADDING, layout_for
    from ui.vecrv import PANEL_WIDTH, SPAN

    v = view()
    v.set_layout(layout_for(SPAN + 2 * BODY_PADDING))
    both = v.position.width
    v.set_layout(layout_for(SPAN + 2 * BODY_PADDING - 1))

    assert both == SPAN
    assert v.position.width == v.band.width == PANEL_WIDTH


def test_the_note_takes_its_paper_from_the_theme_on_screen() -> None:
    import flet as ft

    from ui import theme

    class Page:
        def __init__(self, name: str) -> None:
            self.theme, self.theme_mode = theme.theme_for(name)

    papers = {name: theme.sticky_bg(Page(name)) for name in theme.NAMES}

    assert papers["light"] == theme.STICKY_LIGHT
    assert papers["dark"] == theme.STICKY_DARK
    assert papers["chad"] == theme.STICKY_CHAD
    assert len(set(papers.values())) == len(theme.NAMES)
    assert ft.ThemeMode.DARK is theme.theme_for("dark")[1]


class Recorder:
    """A contract whose chain only moves once the transaction is mined.

    Which is the whole of the bug: asked before that, it truthfully answers
    with the figures that are already on screen.
    """

    can_send = True
    provider = object()

    def __init__(self, log: list[str], before, after) -> None:
        self._log, self._before, self._after = log, before, after

    def is_collecting(self) -> bool:
        return False

    async def claim(self) -> str:
        self._log.append("send")
        return "0x" + "ab" * 32

    async def snapshot(self):
        self._log.append("read")
        return self._after if "wait" in self._log else self._before


def run_claim(monkeypatch, waiter) -> tuple[list[str], object]:
    """Click Claim on a view holding 7 crvUSD, with `waiter` as the wait."""
    import asyncio

    from curve.vecrv import Snapshot
    from ui import vecrv as ui_vecrv

    log: list[str] = []
    before = Snapshot(lock=Lock(), claimable=7 * 10**18)
    after = Snapshot(lock=Lock(amount=10**18, end=int(NOW) + 90 * 86400),
                     claimable=0)
    contract = Recorder(log, before, after)

    async def waited(provider, tx, **kw):
        log.append("wait")
        return await waiter(provider, tx)

    monkeypatch.setattr(ui_vecrv, "wait_for_confirmation", waited)
    v = ui_vecrv.VeCrvView(_StubPage(), contract_for=lambda: contract,
                           now=lambda: NOW)
    v.show(before)
    asyncio.run(v._claim(None))
    return log, v


class _StubPage:
    def update(self) -> None: ...
    def run_task(self, *_a, **_k) -> None: ...


def test_the_figures_are_read_back_only_once_the_claim_is_mined(monkeypatch)\
        -> None:
    """Read before the receipt and the page redraws what was already on it."""
    async def mined(_provider, _tx):
        return 777

    log, v = run_claim(monkeypatch, mined)

    assert log == ["send", "wait", "read"]
    assert v.claimable.value == "0 crvUSD"


def test_and_not_at_all_when_it_never_lands(monkeypatch) -> None:
    """A pending transaction has moved nothing, so nothing is re-read."""
    from curve.confirm import StillPending

    async def never(_provider, _tx):
        raise StillPending("0xabab… has not been mined yet.")

    log, v = run_claim(monkeypatch, never)

    assert log == ["send", "wait"]
    assert "has not been mined" in v.status.text.value
    assert not v.claim_button.disabled  # and the panel is usable again
