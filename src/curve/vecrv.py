"""Locking CRV for veCRV, and claiming what the lock earns.

Two contracts, both on Ethereum and nowhere else, which is why every read
here takes a provider already pointed at it rather than a chain id.

**The approval is exact, and here that is not merely house style.**  Every
other write in this app approves the amount it is about to spend because an
unlimited allowance is a standing risk; the escrow makes it a live one.
`deposit_for(addr, value)` is public and takes the coins from `addr`, so an
address that has left an infinite allowance sitting on the escrow can have
its CRV locked by anybody, on the lock it already has, until that lock ends.
`build_approve` therefore has no unlimited path to reach for.

The distributor's `claim` is not a `view` and answers the amount it moved, so
one `eth_call` against it is both the preview and a dry run of the send --
see `claimable`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from wallet import batch
from wallet.base import RpcError, WalletError, WalletProvider

from . import abi
from .multicall import MULTICALL3, decode_aggregate3, encode_aggregate3

#: The voting escrow, and the distributor that pays its holders.
VOTING_ESCROW = "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"
FEE_DISTRIBUTOR = "0xD16d5eC345Dd86Fb63C6a9C43c517210F1027914"

#: What is locked, and what the distributor pays out.  Both read off the
#: contracts themselves in `tests/fork`; spelled here so a panel can name
#: them before either has answered.
CRV = "0xD533a949740bb3306d119CC777fa900bA034cd52"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"

#: veCRV rounds every unlock time down to a Thursday, so the app rounds it
#: the same way.  Without this the date on screen and the date on chain
#: differ by up to a week, and the one on screen is the wrong one.
WEEK = 7 * 24 * 60 * 60

#: Four years, the longest lock the escrow accepts.  Not read from the
#: contract: `MAXTIME()` is not a getter on this deployment -- it reverts --
#: so the contract stays the authority and this is only what the UI offers.
MAXTIME = 4 * 365 * 24 * 60 * 60


class VeCrvError(WalletError):
    """Something the veCRV page needed could not be done.

    A `WalletError` like `PoolCallFailed`, so a panel that already catches
    those catches these without knowing this module exists.
    """


def week_floor(when: int) -> int:
    """`when`, rounded down to the week boundary the escrow rounds to."""
    return int(when) // WEEK * WEEK


@dataclass(frozen=True, slots=True)
class Lock:
    """What one address has in the escrow."""

    amount: int = 0
    end: int = 0

    @property
    def exists(self) -> bool:
        return self.amount > 0

    def expired(self, now: float) -> bool:
        """Past its end -- so it can be withdrawn and cannot be added to."""
        return self.exists and self.end <= now

    def seconds_left(self, now: float) -> int:
        return max(0, int(self.end - now))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the page draws, as one answer."""

    lock: Lock
    voting_power: int = 0
    total_voting_power: int = 0
    crv: int = 0
    allowance: int = 0
    claimable: int = 0

    @property
    def share(self) -> float:
        """This address's share of all voting power, as a percent."""
        if self.total_voting_power <= 0:
            return 0.0
        return self.voting_power / self.total_voting_power * 100.0


class VeCrvContract:
    """The escrow and the distributor, from a wallet's point of view."""

    def __init__(self, provider: WalletProvider, account: str) -> None:
        self.provider = provider
        self.account = account
        #: Set while `collecting` is open; see `PoolContract.collecting`,
        #: which this mirrors so the same batching path works here.
        self._collected: list[batch.Call] | None = None

    @property
    def can_send(self) -> bool:
        return bool(self.account)

    # -- reads -------------------------------------------------------------

    async def _read(self, to: str, data: str, what: str) -> str:
        try:
            return await self.provider.call(to, data)
        except RpcError as exc:
            raise VeCrvError(f"Could not read {what}: {exc.message}") from exc

    async def locked(self) -> Lock:
        """The amount and the end date, as one answer."""
        raw = await self._read(
            VOTING_ESCROW, abi.encode_locked(self.account), "the lock"
        )
        amount, end = abi.decode_locked(raw)
        return Lock(max(0, amount), end)

    async def voting_power(self) -> int:
        """veCRV held now, which decays to nothing by the lock's end."""
        from wallet.erc20 import encode_balance_of

        raw = await self._read(
            VOTING_ESCROW, encode_balance_of(self.account), "the veCRV balance"
        )
        return abi.decode_uint(raw)

    async def total_voting_power(self) -> int:
        raw = await self._read(
            VOTING_ESCROW, abi.encode_total_supply(), "the veCRV supply"
        )
        return abi.decode_uint(raw)

    async def crv_balance(self) -> int:
        from wallet.erc20 import encode_balance_of

        raw = await self._read(
            CRV, encode_balance_of(self.account), "the CRV balance"
        )
        return abi.decode_uint(raw)

    async def allowance(self) -> int:
        """What the escrow is currently allowed to take."""
        raw = await self._read(
            CRV,
            abi.encode_allowance(self.account, VOTING_ESCROW),
            "the CRV allowance",
        )
        return abi.decode_uint(raw)

    async def claimable(self) -> int:
        """crvUSD waiting for this address.

        `claim` is not a `view`, so this is the send itself run as a call:
        the number it answers is the number it would move, which is a
        stronger preview than a separate estimator that can disagree with it.
        """
        raw = await self._read(
            FEE_DISTRIBUTOR,
            abi.encode_claim(self.account),
            "the claimable amount",
        )
        return abi.decode_uint(raw)

    async def snapshot(self) -> Snapshot:
        """Every figure the page draws, in one round trip where there is one.

        Six reads against three contracts, which is six serial round trips
        asked one at a time -- and this page asks for all of them at once or
        not at all.  `aggregate3` takes the targets as well as the calls, so
        the whole page is one request; the loop underneath is the fallback
        for a node without Multicall3, not the normal path.

        `claim` goes through it too.  It reads `msg.sender` nowhere -- the
        address it pays is the argument -- so it answers the same through the
        aggregator as it does alone, which `tests/fork` checks rather than
        assumes.
        """
        from wallet.erc20 import encode_balance_of

        if not self.account:
            # Nothing to ask about.  Reading for the empty address would
            # throw out of `_address` before any of this is sent, and a page
            # with no wallet on it wants an empty answer rather than a
            # traceback about a string.
            return Snapshot(lock=Lock())
        plan = [
            (VOTING_ESCROW, abi.encode_locked(self.account)),
            (VOTING_ESCROW, encode_balance_of(self.account)),
            (VOTING_ESCROW, abi.encode_total_supply()),
            (CRV, encode_balance_of(self.account)),
            (CRV, abi.encode_allowance(self.account, VOTING_ESCROW)),
            (FEE_DISTRIBUTOR, abi.encode_claim(self.account)),
        ]
        answers = await self._read_many(plan)
        amount, end = abi.decode_locked(answers[0] or "0x")
        def one(index: int) -> int:
            raw = answers[index]
            return abi.decode_uint(raw) if raw else 0
        return Snapshot(
            lock=Lock(max(0, amount), end),
            voting_power=one(1),
            total_voting_power=one(2),
            crv=one(3),
            allowance=one(4),
            claimable=one(5),
        )

    async def _read_many(self, plan: list[tuple[str, str]]) -> list[str | None]:
        """The batch, or one call each where there is no aggregator."""
        with contextlib.suppress(Exception):
            raw = await self.provider.call(MULTICALL3, encode_aggregate3(plan))
            values = decode_aggregate3(raw)
            if len(values) == len(plan):
                return values
        out: list[str | None] = []
        for to, data in plan:
            try:
                out.append(await self.provider.call(to, data))
            except RpcError:
                out.append(None)
        return out

    # -- writes ------------------------------------------------------------

    def build_approve(self, amount: int) -> tuple[str, str]:
        """Allow the escrow exactly `amount`, and never more.

        There is no unlimited option here on purpose -- see this module's
        docstring: `deposit_for` is public, so an infinite allowance on the
        escrow is one anybody may spend on your behalf.
        """
        if amount <= 0:
            raise VeCrvError("An approval has to name an amount.")
        return CRV, abi.encode_approve(VOTING_ESCROW, amount)

    def build_create_lock(self, amount: int, unlock_time: int) -> tuple[str, str]:
        return VOTING_ESCROW, abi.encode_create_lock(amount, week_floor(unlock_time))

    def build_increase_amount(self, amount: int) -> tuple[str, str]:
        return VOTING_ESCROW, abi.encode_increase_amount(amount)

    def build_increase_unlock_time(self, unlock_time: int) -> tuple[str, str]:
        return VOTING_ESCROW, abi.encode_increase_unlock_time(week_floor(unlock_time))

    def build_withdraw(self) -> tuple[str, str]:
        return VOTING_ESCROW, abi.encode_ve_withdraw()

    def build_claim(self) -> tuple[str, str]:
        if not self.account:
            raise VeCrvError("Connect a wallet first.")
        return FEE_DISTRIBUTOR, abi.encode_claim(self.account)

    async def approve(self, amount: int) -> str:
        return await self._send(*self.build_approve(amount))

    async def create_lock(self, amount: int, unlock_time: int) -> str:
        return await self._send(*self.build_create_lock(amount, unlock_time))

    async def increase_amount(self, amount: int) -> str:
        return await self._send(*self.build_increase_amount(amount))

    async def increase_unlock_time(self, unlock_time: int) -> str:
        return await self._send(*self.build_increase_unlock_time(unlock_time))

    async def withdraw(self) -> str:
        return await self._send(*self.build_withdraw())

    async def claim(self) -> str:
        return await self._send(*self.build_claim())

    async def _send(self, to: str, data: str) -> str:
        if self._collected is not None:
            self._collected.append(batch.Call(to, data))
            return ""
        if not self.can_send:
            raise WalletError("Connect a wallet first.")
        return await self.provider.send_transaction(
            {"from": self.account, "to": to, "value": "0x0", "data": data}
        )

    @property
    def is_collecting(self) -> bool:
        return self._collected is not None

    @contextlib.contextmanager
    def collecting(self):
        """Record what an action would send, rather than sending it."""
        held, self._collected = self._collected, []
        try:
            yield self._collected
        finally:
            self._collected = held
