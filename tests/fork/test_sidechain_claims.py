"""Claiming on a chain whose gauges are minted by a factory, not the Minter.

The revert this is here for came from Arbitrum: a `mint_many` naming a gauge
the factory did not deploy dies on `gauge_data[gauge] == 0`, a bare Vyper
assert, so it reaches a wallet as an empty revert with no reason string.  The
chain table named the wrong factory on six chains and every claim through it
failed that way.

Arbitrum because CRV still reaches its gauges: measured on the current week,
Sonic and BSC gauges have an inflation rate of zero, so a claim there would
mint nothing and prove nothing.
"""

from __future__ import annotations

import pytest

from curve import earnings, portfolio
from curve.api import ApiError, CurveApi
from curve.rewards import REWARDS
from wallet.base import WalletError

pytestmark = pytest.mark.fork

CHAIN_ID = 42161
CRV = REWARDS[CHAIN_ID].crv

#: Found by walking a gauge's Deposit log for an account with CRV waiting --
#: see the finder in the commit message.  What it is owed changes with every
#: block, so nothing here asserts an amount, only that what was read arrives.
STAKER = "0xe704de2876e065a4b05b7be23130eefd84813414"


async def positions(fork, account: str) -> tuple[list, dict]:
    """What the portfolio would show for this account, by its own path."""
    api = CurveApi()
    try:
        targets = await api.portfolio_targets("arbitrum", CHAIN_ID)
    except ApiError as exc:
        pytest.skip(f"the pool list is not available: {exc}")
    # Only the pools with a gauge: about fifty of Arbitrum's five hundred.
    # A claim cannot come from the rest, and every extra target is a cold
    # storage read that anvil fetches from upstream one at a time.
    targets = [target for target in targets if target.gauge]
    provider = fork.provider()
    try:
        holdings = await portfolio.scan(
            provider, targets, account, chain_id=CHAIN_ID)
    except WalletError as exc:
        pytest.skip(f"the fork could not answer the scan: {exc}")
    seeds = [
        earnings.Earning(pool=h.address, gauge=h.gauge, lp_token=h.lp_token,
                         staked=h.staked, wallet=h.wallet)
        for h in holdings if h.gauge
    ]
    if not seeds:
        pytest.skip(f"{account[:10]} holds nothing staked on Arbitrum now")
    try:
        resolved, minters = await earnings.resolve_gauges(provider, CHAIN_ID, seeds)
        filled = await earnings.read_earnings(provider, account, resolved)
    except WalletError as exc:
        pytest.skip(f"the fork could not answer the gauges: {exc}")
    return filled, minters


async def test_a_sidechain_claim_mints_what_the_gauges_said_they_owed(
    arbitrum_fork,
) -> None:
    """End to end, through the portfolio's own path: scan, resolve, read,
    plan, send.  The assertion that matters is that the transactions do not
    revert -- that is the whole failure this fixes -- and after that, that
    the CRV actually moved.
    """
    fork = arbitrum_fork
    # Arbitrum's block gas limit is 2**50, and a transaction sent with no gas
    # field of its own is checked for funds against all of it -- about 1.15M
    # ether at the fork's gas price.  A real wallet fills the field in; anvil
    # is the wallet here, so the account is funded past the check instead.
    fork.give_eth(STAKER, 10_000_000)
    filled, minters = await positions(fork, STAKER)

    owed = sum(r.amount for e in filled for r in e.rewards if r.minted)
    if owed == 0:
        pytest.skip("nothing is owed in CRV on Arbitrum at this block")
    plan = earnings.claim_plan(CHAIN_ID, filled, minters)
    assert plan.crv, "something is owed and the plan claims none of it"

    before = fork.erc20_balance(CRV, STAKER)
    sent = await earnings.send_claims(fork.provider(), STAKER, plan, crv=True)
    for tx in sent:
        fork.wait(tx)          # asserts status 1: the empty revert is the bug

    after = fork.erc20_balance(CRV, STAKER)
    assert after - before >= owed, (
        f"claimed {after - before} against {owed} owed -- the mint went "
        "through a factory that does not know these gauges"
    )


async def test_every_gauge_is_claimed_through_the_factory_that_deployed_it(
    arbitrum_fork,
) -> None:
    """Not through the chain table.  A gauge names its own minter and each
    factory keeps its own `minted[user][gauge]`, so a plan that groups by the
    chain sends one batch that reverts instead of two that pay.
    """
    fork = arbitrum_fork
    filled, minters = await positions(fork, STAKER)
    owing = [e for e in filled if e.has_crv]
    if not owing:
        pytest.skip("nothing is owed in CRV on Arbitrum at this block")

    plan = earnings.claim_plan(CHAIN_ID, filled, minters)
    named = {gauge for _minter, gauges in plan.crv for gauge in gauges}

    assert named == {e.gauge for e in owing}, "a gauge that owed was dropped"
    for minter, gauges in plan.crv:
        for gauge in gauges:
            assert minters.get(gauge.lower(), minter) == minter, (
                f"{gauge} is batched under {minter} but names another minter"
            )
