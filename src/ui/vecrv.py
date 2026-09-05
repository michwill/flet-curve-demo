"""The veCRV page: locking CRV, and claiming what the lock earns.

Two panels over one snapshot.  The escrow's own arithmetic decides what a
lock is worth -- `amount * time_left / MAXTIME`, decaying to nothing at the
end -- and that is worked out here rather than read, because it is a formula
and not a state: the contract will not tell you what a lock you have not made
yet would be worth.

Everything is Ethereum's.  `CurveApp` keeps the page out of the nav on other
chains, so this module never has to ask which one it is on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import flet as ft

from curve.confirm import wait_for_confirmation
from curve.format import token_amount, units_to_float
from curve.models import Coin
from curve.vecrv import (
    CRV,
    CRVUSD,
    MAXTIME,
    WEEK,
    Lock,
    Snapshot,
    VeCrvContract,
    week_floor,
)
from wallet.base import WalletError
from wallet.erc20 import format_units, parse_units

from . import buttons, safe_update, theme
from .actions import amount_field, stacked
from .alarm import Band
from .logos import token_mark
from .responsive import Layout
from .status import DONE, FAILED, StatusPanel
from .typography import BODY, LABEL, METRIC, ROW_TITLE, SMALL

#: The durations the buttons offer, in the order they are drawn.  A month is
#: four weeks rather than a calendar one: the escrow counts in weeks, and
#: "one month" that lands on a different Thursday depending on the month is
#: a worse promise than one that does not.
PRESETS: tuple[tuple[str, int], ...] = (
    ("1w", WEEK),
    ("1mo", 4 * WEEK),
    ("1y", 52 * WEEK),
    ("4y", MAXTIME),
)

#: How the date is typed and shown.  ISO, because a date field that accepts
#: 03/04 has to guess which is the month and will guess wrong for somebody.
DATE_FORMAT = "%Y-%m-%d"

#: The coins this page moves, as `token_mark` wants them.
CRV_COIN = Coin(address=CRV, symbol="CRV", decimals=18)
CRVUSD_COIN = Coin(address=CRVUSD, symbol="crvUSD", decimals=18)

#: Mark size beside an amount field, matching the pool page's panels.
MARK = 20

#: How wide each panel is drawn.  Fixed, so the two sit side by side at any
#: width that has room for both and stack when it does not -- the row wraps
#: rather than squeezing them.
PANEL_WIDTH = 380

#: The two panels side by side, gap included.  What spans them -- the sticky
#: note and the status band -- is drawn to this, so its edges land on theirs.
SPAN = PANEL_WIDTH * 2 + 16

#: How far off square the note sits, in radians.  Enough to read as paper
#: somebody stuck on rather than a box the layout drew.
TILT = -0.006


def voting_power_for(amount: int, seconds: int) -> int:
    """What the escrow would credit for `amount` locked for `seconds`.

    Linear in the time left and zero once there is none, which is the whole
    of `VotingEscrow`'s balance curve -- the contract stores the slope and
    the bias, and this is what they come to.
    """
    if amount <= 0 or seconds <= 0:
        return 0
    return amount * min(seconds, MAXTIME) // MAXTIME


def say_duration(seconds: int) -> str:
    """A span in the units somebody would say it in."""
    if seconds <= 0:
        return "now"
    years, rest = divmod(seconds, 365 * 24 * 3600)
    months, rest = divmod(rest, 30 * 24 * 3600)
    days = rest // (24 * 3600)
    parts = [
        f"{years} year{'s' if years != 1 else ''}" if years else "",
        f"{months} month{'s' if months != 1 else ''}" if months else "",
        f"{days} day{'s' if days != 1 else ''}" if days and not years else "",
    ]
    said = " ".join(p for p in parts if p)
    return said or "less than a day"


def say_date(when: int) -> str:
    """The unlock date, as the escrow will actually have it."""
    if when <= 0:
        return "-"
    return dt.datetime.fromtimestamp(week_floor(when), dt.UTC).strftime(
        "%a %d %b %Y"
    )


class VeCrvView(ft.Column):
    """Both panels, and the figures above them."""

    def __init__(
        self,
        page: ft.Page,
        *,
        contract_for: Callable[[], VeCrvContract | None],
        now: Callable[[], float] = lambda: dt.datetime.now(dt.UTC).timestamp(),
    ) -> None:
        self._page = page
        self._contract_for = contract_for
        self._now = now
        self._snapshot = Snapshot(lock=Lock())
        self._layout: Layout | None = None
        #: The duration the buttons last set, so the date field and the
        #: buttons agree about which one is chosen.
        self._preset: int | None = None
        self._busy = False

        self.status = StatusPanel(page)
        self.position = _Position(page)
        self.amount = amount_field(
            "CRV", self._changed, token_mark(CRV_COIN, "ethereum", MARK),
            on_max=self._max_clicked,
        )
        self.balance_line = ft.Text("", size=LABEL,
                                    color=ft.Colors.ON_SURFACE_VARIANT)
        self.date = ft.TextField(
            label="Unlock date",
            hint_text="2030-09-05",
            dense=True,
            on_change=self._changed,
            # Typing still works -- somebody who knows the date they want
            # should not have to walk a calendar to it -- but the calendar
            # is what a date field is expected to open, and it is the only
            # way to find "the Thursday after next" without counting.
            suffix_icon=ft.IconButton(
                ft.Icons.CALENDAR_MONTH,
                icon_size=18,
                tooltip="Pick a date",
                on_click=self._open_calendar,
            ),
        )
        self.date_line = ft.Text("", size=LABEL,
                                 color=ft.Colors.ON_SURFACE_VARIANT)
        # `buttons.Themed` rather than an `OutlinedButton`: it re-reads the
        # scheme on every update, which is how every other button in the app
        # follows a theme change instead of keeping the one it was built in.
        self._preset_buttons = {
            seconds: buttons.Themed(
                label, page=page,
                on_click=lambda _e, s=seconds: self._preset_clicked(s),
            )
            for label, seconds in PRESETS
        }
        self.gain = Band(
            ft.Text("", size=SMALL), page, kind="impact", visible=False
        )
        self.approve_button = buttons.Themed(
            "Approve", page=page, on_click=self._approve, visible=False
        )
        self.lock_button = buttons.Themed(
            "Create lock", page=page, on_click=self._lock, disabled=True
        )
        self.extend_button = buttons.Themed(
            "Extend lock", page=page, on_click=self._extend, visible=False
        )
        self.withdraw_button = buttons.Themed(
            "Withdraw", page=page, on_click=self._withdraw, visible=False
        )

        self.claimable = ft.Text("-", size=METRIC, weight=ft.FontWeight.BOLD)
        self.claim_button = buttons.Themed(
            "Claim", page=page, on_click=self._claim, disabled=True
        )

        self.band = ft.Container(self.status, width=SPAN)
        super().__init__(
            controls=[
                self.position,
                # As wide as the panels under it and no wider: left to
                # itself the band stretches the window while everything
                # around it is centred.
                self.band,
                ft.Row(
                    [self._lock_panel(), self._claim_panel()],
                    spacing=16,
                    alignment=ft.MainAxisAlignment.CENTER,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    wrap=True,
                    run_spacing=16,
                ),
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

    # -- the two panels ----------------------------------------------------

    def _lock_panel(self) -> ft.Control:
        self.lock_title = ft.Text("Lock CRV", size=ROW_TITLE,
                                  weight=ft.FontWeight.BOLD)
        return self._panel(
            ft.Column(
                [
                    self.lock_title,
                    stacked(self.amount, self.balance_line),
                    ft.Row(list(self._preset_buttons.values()), spacing=8,
                           wrap=True),
                    stacked(self.date, self.date_line),
                    self.gain,
                    ft.Row([self.approve_button, self.lock_button,
                            self.extend_button, self.withdraw_button],
                           spacing=8, wrap=True),
                ],
                spacing=12,
            )
        )

    def _claim_panel(self) -> ft.Control:
        return self._panel(
            ft.Column(
                [
                    ft.Text("Weekly distribution", size=ROW_TITLE,
                            weight=ft.FontWeight.BOLD),
                    ft.Row(
                        [token_mark(CRVUSD_COIN, "ethereum", MARK), self.claimable],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "crvUSD from trading fees, paid to veCRV every Thursday.",
                        size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT,
                    ),
                    self.claim_button,
                ],
                spacing=12,
            )
        )

    def _panel(self, content: ft.Control) -> ft.Control:
        return ft.Container(
            content,
            padding=16,
            width=PANEL_WIDTH,
            bgcolor=ft.Colors.SURFACE,
            border=theme.panel_border(self._page),
            border_radius=10,
            shadow=theme.panel_shadow(self._page),
        )

    def set_layout(self, layout: Layout) -> None:
        self._layout = layout
        # The panel row wraps rather than squeezes, so below the width of
        # two there is one panel for these to line up with, not two.
        span = SPAN if layout.room >= SPAN else PANEL_WIDTH
        self.position.width = self.band.width = span


    # -- what the figures say ----------------------------------------------

    def show(self, snapshot: Snapshot) -> None:
        """Draw a fresh reading, and re-decide what can be done with it."""
        self._snapshot = snapshot
        now = self._now()
        lock, expired = snapshot.lock, snapshot.lock.expired(self._now())
        self.position.show(snapshot, now)
        self.balance_line.value = (
            f"Balance: {token_amount(units_to_float(snapshot.crv, 18))} CRV"
        )
        self.claimable.value = (
            f"{token_amount(units_to_float(snapshot.claimable, 18))} crvUSD"
        )
        self.claim_button.disabled = snapshot.claimable <= 0

        self.lock_title.value = (
            "Withdraw" if expired
            else "Add to your lock" if lock.exists
            else "Lock CRV"
        )
        # An expired lock is the only thing that can be done with it: the
        # escrow refuses both `increase_amount` and `increase_unlock_time`
        # once the end has passed, so offering either would be offering a
        # revert.
        for control in (self.amount, self.date, self.gain):
            control.visible = not expired
        for button in self._preset_buttons.values():
            button.visible = not expired
        self.withdraw_button.visible = expired
        self.withdraw_button.content = (
            f"Withdraw {token_amount(units_to_float(lock.amount, 18))} CRV"
        )
        self.lock_button.visible = not expired
        self.lock_button.content = "Add CRV" if lock.exists else "Create lock"
        self.extend_button.visible = lock.exists and not expired
        self._sync()

    def _sync(self) -> None:
        """The parts that move with what has been typed."""
        snapshot, now = self._snapshot, self._now()
        lock = snapshot.lock
        amount = self._amount()
        until = self._until()
        held = lock.seconds_left(now)
        seconds = max(held, int(week_floor(until) - now)) if until else held

        self.date_line.value = (
            f"{say_date(until)} · {say_duration(int(week_floor(until) - now))}"
            if until else
            f"Locked until {say_date(lock.end)}" if lock.exists else ""
        )
        gain = voting_power_for(lock.amount + amount, seconds)
        self.gain.visible = bool(amount or (until and lock.exists))
        self.gain.content = ft.Text(
            f"You would hold ~{token_amount(units_to_float(gain, 18))} veCRV, "
            f"decaying to zero by {say_date(max(until, lock.end))}",
            size=SMALL,
        )

        for seconds, button in self._preset_buttons.items():
            button.disabled = self._busy or not self.preset_reachable(seconds)

        # Never for more than the wallet holds: that allowance could not be
        # spent on a lock anyway, and it would stand afterwards -- which on
        # this escrow is an amount anybody may lock on your behalf.
        needs = max(0, amount - snapshot.allowance)
        self.approve_button.visible = (
            needs > 0 and amount <= snapshot.crv and not lock.expired(now)
        )
        self.approve_button.content = (
            f"Approve {token_amount(units_to_float(amount, 18))} CRV"
        )
        self.amount.error = self._amount_error()
        self.lock_button.disabled = self._why_not_lock() is not None
        self.extend_button.disabled = self._why_not_extend() is not None
        safe_update(self)

    def _amount_error(self) -> str | None:
        """What is wrong with what is typed, for the field to say itself.

        A number the field cannot read is read as nothing, and nothing
        disables every button on the panel -- which from the outside looks
        like the page is broken rather than like the amount is.
        """
        text = (self.amount.value or "").strip()
        if not text:
            return None
        try:
            amount = parse_units(text, 18)
        except ValueError as exc:
            return str(exc)
        if amount > self._snapshot.crv:
            return "More than the wallet holds"
        return None

    def _why_not_lock(self) -> str | None:
        """Why the lock button is dead, or None if it is not."""
        snapshot, now = self._snapshot, self._now()
        amount = self._amount()
        if self._busy or not amount:
            return "no amount"
        if amount > snapshot.crv:
            return "more than the wallet holds"
        if amount > snapshot.allowance:
            return "not approved yet"
        if snapshot.lock.exists:
            return None if not snapshot.lock.expired(now) else "the lock has ended"
        until = self._until()
        if not until or week_floor(until) <= now:
            return "no unlock date"
        return None

    def _why_not_extend(self) -> str | None:
        snapshot, now = self._snapshot, self._now()
        until = self._until()
        if self._busy or not snapshot.lock.exists or snapshot.lock.expired(now):
            return "no lock to extend"
        if not until or week_floor(until) <= snapshot.lock.end:
            return "not later than it already is"
        if week_floor(until) > now + MAXTIME:
            return "further out than four years"
        return None

    # -- reading the fields ------------------------------------------------

    def _amount(self) -> int:
        try:
            return parse_units((self.amount.value or "").strip(), 18)
        except ValueError:
            return 0

    def _until(self) -> int:
        """The typed date as a timestamp, or 0 where there is not one yet."""
        text = (self.date.value or "").strip()
        if not text:
            return 0
        try:
            when = dt.datetime.strptime(text, DATE_FORMAT).replace(tzinfo=dt.UTC)
        except ValueError:
            return 0
        return int(when.timestamp())

    # -- what the reader does ----------------------------------------------

    def _open_calendar(self, _e) -> None:
        """A calendar between now and the four years the escrow allows."""
        now = self._now()
        floor = max(now + WEEK, float(self._snapshot.lock.end))
        picker = ft.DatePicker(
            value=dt.datetime.fromtimestamp(self._until() or floor, dt.UTC),
            first_date=dt.datetime.fromtimestamp(floor, dt.UTC),
            last_date=dt.datetime.fromtimestamp(now + MAXTIME, dt.UTC),
            help_text="Unlock date",
            on_change=self._calendar_picked,
        )
        self._page.show_dialog(picker)

    def _calendar_picked(self, e) -> None:
        chosen = getattr(e.control, "value", None)
        if chosen is None:
            return
        # Shown as the escrow will have it, not as it was clicked: the
        # calendar offers every day and the escrow keeps Thursdays.
        self.date.value = dt.datetime.fromtimestamp(
            week_floor(int(chosen.timestamp())), dt.UTC
        ).strftime(DATE_FORMAT)
        self._preset = None
        self._sync()

    def _changed(self, _e) -> None:
        self._preset = None
        self._sync()

    def preset_date(self, seconds: int) -> int:
        """The unlock date "1y" and the rest stand for.

        Measured from now, not from the end already there: the buttons say
        "1y", and a lock that ends in a year is what that means whether or
        not one exists.  Reading them as extensions made "1y" on a
        three-year lock mean four, which is not what the label says and is
        why the shorter ones looked available when they are not.
        """
        return week_floor(int(min(self._now() + seconds, self._now() + MAXTIME)))

    def preset_reachable(self, seconds: int) -> bool:
        """Whether that date is one the escrow would accept.

        A lock only ever moves outwards.  With three years left, "1y" names a
        date in the past as far as the escrow is concerned and
        `increase_unlock_time` refuses it -- so the button is dead rather
        than there to be pressed and told no.
        """
        when = self.preset_date(seconds)
        if when <= self._now():
            return False
        return when > self._snapshot.lock.end if self._snapshot.lock.exists else True

    def _preset_clicked(self, seconds: int) -> None:
        """Set the date from a button."""
        if not self.preset_reachable(seconds):
            return
        self._preset = seconds
        self.date.value = dt.datetime.fromtimestamp(
            self.preset_date(seconds), dt.UTC
        ).strftime(DATE_FORMAT)
        self._sync()

    def _max_clicked(self, _e) -> None:
        # `format_units`, as every other MAX in the app does it: exact, and
        # never through a float.  `token_amount` is for *reading* -- it
        # rounds, groups the thousands and can run past 18 decimals, and the
        # field it was filling is read back with `parse_units`, which refuses
        # all three.  That refusal is swallowed as "no amount", which is why
        # pressing MAX disabled every button on the panel.
        self.amount.value = format_units(self._snapshot.crv, 18, precision=18)
        self._sync()

    async def _approve(self, _e) -> None:
        await self._step(
            "Approving…",
            lambda c: c.approve(self._amount()),
            f"Approved {token_amount(units_to_float(self._amount(), 18))} CRV.",
        )

    async def _lock(self, _e) -> None:
        amount = self._amount()
        if self._snapshot.lock.exists:
            await self._step("Adding to the lock…",
                             lambda c: c.increase_amount(amount),
                             "Added to your lock.")
            return
        until = self._until()
        await self._step("Creating the lock…",
                         lambda c: c.create_lock(amount, until),
                         f"Locked until {say_date(until)}.")

    async def _extend(self, _e) -> None:
        until = self._until()
        await self._step("Extending the lock…",
                         lambda c: c.increase_unlock_time(until),
                         f"Locked until {say_date(until)}.")

    async def _withdraw(self, _e) -> None:
        await self._step("Withdrawing…",
                         lambda c: c.withdraw(),
                         "Withdrawn.")

    async def _claim(self, _e) -> None:
        amount = self._snapshot.claimable
        await self._step(
            "Claiming…",
            lambda c: c.claim(),
            f"Claimed {token_amount(units_to_float(amount, 18))} crvUSD.",
        )

    async def _step(self, saying: str, send, done: str) -> None:
        """One transaction, from the prompt to the figures it moves.

        The wait is the point.  `send` is finished the moment the wallet
        hands back a hash, and until that hash is mined every figure here is
        read from a chain that has not run it: re-reading there redraws what
        was already on screen, which is what a claim leaving its own
        claimable amount standing looked like.  `wait_for_confirmation` also
        waits for the endpoint to reach the block, so the read afterwards
        cannot land on a node that is still behind it.
        """
        contract = self._contract_for()
        if contract is None or not contract.can_send:
            self.status.say("Connect a wallet first.", FAILED)
            return
        self._busy = True
        self._sync()
        self.status.say(saying, pending=True)
        try:
            tx = await send(contract)
            # Empty while a batch is being collected: nothing has been sent,
            # so there is nothing to wait for and nothing has moved.
            if tx:
                self.status.say(f"Waiting for {tx[:14]}… to confirm.",
                                pending=True)
                await wait_for_confirmation(contract.provider, tx)
        except WalletError as exc:
            self.status.say(str(exc), FAILED, sticky=True)
            return
        finally:
            self._busy = False
            # Synced here rather than left to `reload`, which a failure
            # never reaches -- and which is what kept the buttons disabled
            # after one.
            self._sync()
        self.status.say(done, DONE, sticky=True)
        await self.reload()

    async def reload(self) -> None:
        """Read everything again, which is one request."""
        contract = self._contract_for()
        if contract is None:
            return
        try:
            self.show(await contract.snapshot())
        except WalletError as exc:
            self.status.say(str(exc), FAILED)
            return
        # An empty snapshot on its own reads as "you have none of this"
        # rather than "nobody has said who you are" -- and the moment a
        # wallet arrives, saying so is worse than saying nothing.
        self.status.say(
            "" if contract.can_send else "Connect a wallet to lock CRV or claim."
        )


class _Position(ft.Container):
    """What this address already holds, above the panels."""

    def __init__(self, page: ft.Page) -> None:
        self.power = ft.Text("-", size=METRIC, weight=ft.FontWeight.BOLD)
        self.share = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
        self.locked = ft.Text("-", size=BODY)
        self.until = ft.Text("", size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT)
        super().__init__(
            ft.Row(
                [
                    ft.Column([ft.Text("Voting power", size=LABEL,
                                       color=ft.Colors.ON_SURFACE_VARIANT),
                               self.power, self.share], spacing=2, tight=True),
                    ft.Column([ft.Text("Locked", size=LABEL,
                                       color=ft.Colors.ON_SURFACE_VARIANT),
                               self.locked, self.until], spacing=2, tight=True),
                ],
                spacing=72,
                run_spacing=12,
                # Wrapped rather than clipped: the two figures are long --
                # nine digits of veCRV and a date -- and a narrow window was
                # cutting the second one off the end of the row.
                wrap=True,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
            # A note, not a panel: these are what is already true, and the
            # panels below are what can be done about it.  Bordered like one
            # of them, the two read as a pair of controls, one of which
            # happened to have no buttons.
            width=SPAN,
            padding=ft.Padding.symmetric(horizontal=18, vertical=14),
            border_radius=2,
            margin=ft.Margin.only(bottom=6),
            rotate=ft.Rotate(TILT, alignment=ft.Alignment.CENTER),
        )
        self._page = page

    def before_update(self) -> None:
        """Take the paper and the lift from whichever theme is on screen."""
        super().before_update()
        self.bgcolor = theme.sticky_bg(self._page)
        self.shadow = theme.paper_shadow(self._page)

    def show(self, snapshot: Snapshot, now: float) -> None:
        lock = snapshot.lock
        self.power.value = (
            f"{token_amount(units_to_float(snapshot.voting_power, 18))} veCRV"
        )
        self.share.value = (
            f"{snapshot.share:.4f}% of all voting power"
            if snapshot.voting_power else ""
        )
        self.share.visible = bool(self.share.value)
        self.locked.value = (
            f"{token_amount(units_to_float(lock.amount, 18))} CRV"
            if lock.exists else "nothing"
        )
        self.until.value = (
            "ended, ready to withdraw" if lock.expired(now)
            else f"until {say_date(lock.end)} · {say_duration(lock.seconds_left(now))}"
            if lock.exists else ""
        )
