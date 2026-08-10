"""A forked mainnet, driven through the app's own wallet transport.

Every other test in this repo fakes the chain. These do not: they run the
real calldata against real deployed contracts, on a fork, and check what
actually moved. That is the only way to test the half of this app that
writes -- and it was the untested half, because a wrong `add_liquidity`
selector or an allowance granted to the wrong spender costs money rather
than a redraw, so nobody runs those by hand twice.

**The seam is already there.** `wallet.desktop` talks plain JSON-RPC over
HTTP to Frame or qeth at `127.0.0.1:1248`, and takes its endpoint from
`FLET_PAY_RPC`. Anvil speaks the same protocol, and with
`--auto-impersonate` it will sign as *any* address -- so pointing that env
var at a fork gives the app a wallet that holds anybody's position. No mock
provider, no test double: `DesktopWalletProvider` is the same class the
desktop build uses, which means these tests exercise the transport too.

Marked `fork`, and excluded from the default run: they need the anvil
binary and a mainnet endpoint to fork from, and they take a few seconds
each. Run them with

    pytest -m fork

Set `FORK_RPC_URL` to fork from a node you trust; without it the fixture
picks a public endpoint from the same chainlist directory `curve.rpc` uses,
which is fine but slower and occasionally rate-limited.

**The fork is taken at head, not at a pinned block**, and that is a
deliberate default rather than an oversight. Pinning needs an archive node:
public endpoints keep roughly the last 128 blocks of state and refuse
anything older, so a block number committed here would work for a few
minutes and then skip every run for everyone.

The cost is that mainnet moves under the tests -- which is not theoretical,
it is what made the claim tests pass twice and then fail, because the
account they act as claims its rewards every few blocks. So the suite is
built not to care: `Fork.advance` manufactures the accrual it needs rather
than hoping to find it, and nothing asserts on an absolute amount.

When a run does fail and the state mattered, the block it forked at is
printed at session start:

    FORK_BLOCK=25719208 pytest -m fork

That reproduces it exactly, given an endpoint with the history -- which is
what `FORK_RPC_URL` is for.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from curve.rpc import ChainlistDirectory
from wallet.desktop import DesktopWalletProvider
from wallet.erc20 import encode_transfer

#: How long to wait for anvil to answer after launching it. A fork has to
#: reach out to the upstream node before it serves anything, so this is
#: generous compared with starting a bare chain.
STARTUP_TIMEOUT = 90.0

#: Anvil's own unlocked accounts are irrelevant here -- every test acts as
#: some mainnet address -- but one is handy for paying gas from.
ANVIL_MNEMONIC_ACCOUNT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _rpc(url: str, method: str, params: list | None = None, timeout: float = 60.0):
    """One JSON-RPC call, stdlib only, raising on an error result."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if "error" in payload:
        raise AssertionError(f"{method}: {payload['error']}")
    return payload.get("result")


async def _fork_source() -> str:
    """Where to fork from: the env var, else a public endpoint."""
    configured = os.environ.get("FORK_RPC_URL")
    if configured:
        return configured
    directory = ChainlistDirectory()
    endpoints = await directory.endpoints(1)
    if not endpoints:
        pytest.skip("no mainnet endpoint to fork from; set FORK_RPC_URL")
    return endpoints[0]


@pytest.fixture(scope="session")
def anvil() -> str:
    """A forked mainnet on a free port. Yields its URL."""
    binary = shutil.which("anvil") or str(Path.home() / ".config/.foundry/bin/anvil")
    if not Path(binary).exists():
        pytest.skip("anvil not installed (foundry)")

    source = asyncio.run(_fork_source())
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    pinned = os.environ.get("FORK_BLOCK")
    process = subprocess.Popen(
        [
            binary,
            "--fork-url", source,
            "--port", str(port),
            "--auto-impersonate",   # sign as anybody: that is the whole trick
            "--silent",
            *(("--fork-block-number", pinned) if pinned else ()),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() or b"").decode()[-400:]
            pytest.skip(f"anvil exited: {stderr}")
        try:
            if _rpc(url, "eth_chainId", timeout=3.0):
                break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.5)
    else:
        process.terminate()
        pytest.skip(f"anvil did not start within {STARTUP_TIMEOUT}s")

    # Printed, always, because without it a failure is not reproducible: the
    # fork is taken at head unless FORK_BLOCK says otherwise, and mainnet
    # will have moved on by the time anybody reads the traceback. pytest
    # shows this under "Captured stdout setup" for the first failing test,
    # which is exactly when it is wanted.
    at = int(_rpc(url, "eth_blockNumber"), 16)
    print(f"\nforked mainnet at block {at} -- reproduce with FORK_BLOCK={at}")

    yield url

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


class Fork:
    """The bits of a fork a test needs, beyond plain RPC."""

    def __init__(self, url: str) -> None:
        self.url = url

    def rpc(self, method: str, params: list | None = None):
        return _rpc(self.url, method, params)

    def wait(self, tx: str, timeout: float = 30.0, *, require_success: bool = True) -> dict:
        """The receipt, once there is one, asserting it did not revert.

        Polled rather than read once. `eth_sendTransaction` returns as soon
        as the transaction is accepted, and even an auto-mining anvil puts
        it in a block a moment later -- so a single read is a race, and it
        is the race that failed four of these the first time they ran. The
        app has the same rule for the same reason; see `curve.confirm`.

        `require_success=False` for the tests whose point is that something
        *did* revert -- a claim batch that refuses to hide a failed call.
        """
        deadline = time.monotonic() + timeout
        while True:
            receipt = self.rpc("eth_getTransactionReceipt", [tx])
            if receipt is not None:
                assert not require_success or int(receipt["status"], 16) == 1, (
                    f"{tx} reverted"
                )
                return receipt
            assert time.monotonic() < deadline, f"{tx} not mined within {timeout}s"
            time.sleep(0.2)

    def provider(self, timeout: float = 120.0) -> DesktopWalletProvider:
        """The app's real transport, pointed at the fork.

        Not a stub. This is what the desktop build talks to Frame with, so
        a test that passes here has exercised the encoding, the transport
        and the contract together.
        """
        return DesktopWalletProvider(self.url)

    def give_eth(self, address: str, ether: int = 100) -> None:
        self.rpc("anvil_setBalance", [address, hex(ether * 10**18)])

    def warm(self, to: str, data: str) -> None:
        """Make a call once through raw RPC, before the app makes it.

        A fresh fork holds no storage. The first `eth_call` that touches a
        contract fetches every slot it reads from the upstream node, one
        round trip each, and `claimable_tokens` reads a great many -- so
        that first call is slow in a way no later one is.

        Slow enough to trip `wallet.desktop`'s read timeout, which surfaces
        as `WalletUnavailable`, which `ClaimTab.refresh` catches and reports
        as *nothing owed*. The panel then looked exactly as it would for an
        account with no rewards, and the claim test failed with the CRV
        figure at zero while the reads after it -- against warm state --
        returned fine.

        Doing it here, with a timeout suited to a cold fork, keeps that
        accident out of the assertions. It is a property of forking, not of
        the app: on a real node every one of these is warm.
        """
        self.rpc("eth_call", [{"to": to, "data": data}, "latest"])

    def advance(self, seconds: int = 7 * 24 * 3600) -> None:
        """Move the clock forward so time-based rewards accrue.

        Gauge emissions are per second, so what an account is owed depends
        entirely on how long since it last claimed -- and the fixture
        account turned out to be a bot that claims every few blocks. A fork
        taken at an arbitrary mainnet block therefore found it owed
        anything between zero and one block's worth, and the claim tests
        passed or failed depending on the minute they ran.

        Advancing the clock removes the dependency: after a week, any live
        staked position is owed something worth asserting on, whoever it
        belongs to and whenever the fork was taken.
        """
        self.rpc("evm_increaseTime", [seconds])
        self.rpc("evm_mine")

    def snapshot(self) -> str:
        return self.rpc("evm_snapshot")

    def revert(self, snapshot: str) -> None:
        """Roll back, and insist that it happened.

        **A snapshot is spent by the revert that uses it**, along with
        every snapshot taken after it. Reverting to the same id twice is
        not an error: anvil answers `false` and leaves the chain exactly
        where it was. A comparison written as "revert, act, revert, act"
        therefore runs its second act against the first one's leftovers,
        which is how a claim that works came to be recorded as a claim
        that does not -- the second attempt had nothing left to claim.
        """
        assert self.rpc("evm_revert", [snapshot]) is True, (
            f"evm_revert({snapshot}) refused: a snapshot is freed by the "
            "revert that uses it, so take a new one before rolling back again"
        )

    def erc20_balance(self, token: str, owner: str) -> int:
        from wallet.erc20 import encode_balance_of

        raw = self.rpc(
            "eth_call", [{"to": token, "data": encode_balance_of(owner)}, "latest"]
        )
        return int(raw, 16) if raw and raw != "0x" else 0

    def fund_erc20(self, token: str, source: str, to: str, amount: int) -> None:
        """Move tokens from whoever already has them.

        `--auto-impersonate` means any address can sign, so funding needs no
        knowledge of a token's storage layout -- which is the usual reason
        this kind of fixture is brittle. The source is normally the pool
        itself: it holds both coins by definition, and its own accounting
        lives in `balances()` rather than in the token, so taking some does
        not disturb what it quotes.
        """
        self.give_eth(source, 1)
        tx = self.rpc(
            "eth_sendTransaction",
            [{"from": source, "to": token, "data": encode_transfer(to, amount)}],
        )
        receipt = self.rpc("eth_getTransactionReceipt", [tx])
        assert receipt and int(receipt["status"], 16) == 1, f"funding failed: {tx}"


@pytest.fixture
def fork(anvil: str):
    """A fork, rolled back to its starting state after each test."""
    handle = Fork(anvil)
    snapshot = handle.snapshot()
    yield handle
    handle.revert(snapshot)
