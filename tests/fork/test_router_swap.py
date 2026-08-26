"""A swap through the router, on a fork, and then the swap straight after it.

The second one is the point.  A swap of ours moves the pools it went through,
so what happens next depends on the app re-reading them -- from a node that has
actually seen it.  Reading from one still behind prices the next quote on pools
this swap has already moved, and the plan built from it is refused by a dry run
that *did* wait for the block.  See `RouterHost.after_swap`.
"""

from __future__ import annotations

import pytest

from curve.router_contract import RouterContract
from router.host import RouterHost, Stage
from router.session import build_session

pytestmark = pytest.mark.fork

#: An address with ether on mainnet, acted as through anvil's impersonation.
TRADER = "0x39415255619783A2E71fcF7d8f708A951d92e1b6"

#: The pair the reports came in on.  ETH is the native sentinel, so the route
#: opens by wrapping and needs no approval -- which keeps this about the state
#: and not about allowances.
SELL, BUY = "ETH", "DOLA"

#: 0.1 ETH, the size that was being swapped when this was reported.
AMOUNT = 10**17

ETHEREUM = 1


async def _coin(coins, symbol: str):
    for entry in coins:
        if entry.symbol.upper() == symbol.upper():
            return entry
    raise AssertionError(f"{symbol} is not in the router's coin list")


async def _warmed(fork, api, backend):
    """A host with one warmed session, reading and writing the fork."""
    stages: list[Stage] = []

    async def make(chain_id: int):
        return await build_session(chain_id, backend, api=api, rpc_override=fork.url)

    host = RouterHost(make_session=make, on_stage=lambda stage, _e: stages.append(stage))
    await host.open(ETHEREUM)
    assert host.stage is Stage.READY, f"the warm did not finish: {host.error}"
    return host


async def test_a_swap_and_the_swap_straight_after_it(fork, router_api, router_backend):
    fork.give_eth(TRADER, 10)
    host = await _warmed(fork, router_api, router_backend)
    sell, buy = await _coin(host.coins, SELL), await _coin(host.coins, BUY)
    assert await host.set_pair(sell.address, buy.address), "the pair would not prepare"

    session = host.session
    result = session.quote(AMOUNT)
    assert result.route is not None, "no route to trade"

    plan = await session.plan_call(
        result, receiver=TRADER, sender=TRADER, slippage_bp=None, min_out_bp=0.0)
    assert not plan.reverted, f"the first plan would not go through: {plan.reverted}"

    contract = RouterContract(fork.provider(), TRADER)
    before = fork.erc20_balance(buy.address, TRADER)
    receipt = fork.wait(await contract.execute(plan))
    landed = int(receipt["blockNumber"], 16)
    after = fork.erc20_balance(buy.address, TRADER)
    assert after > before, "the swap produced nothing"

    # What the app does on a confirmed swap, with the block it landed in.
    swept = await host.after_swap(landed)
    assert swept >= landed, "re-read from a node that had not seen our own swap"

    # And now the swap that used to come back "below min_out": the pools moved,
    # and everything downstream has to be priced on where this swap left them.
    again = session.quote(AMOUNT)
    assert again.route is not None, "no route the second time"
    second = await session.plan_call(
        again, receiver=TRADER, sender=TRADER, slippage_bp=None, min_out_bp=0.0,
        not_before=landed)
    assert not second.reverted, (
        f"the swap straight after a confirmed one was refused: {second.reverted}"
    )


async def test_a_named_budget_binds_the_whole_route_on_a_fork(
        fork, router_api, router_backend):
    """`min_out` is the promise about the number on screen, and it is the one
    a swap right after another used to break.
    """
    fork.give_eth(TRADER, 10)
    host = await _warmed(fork, router_api, router_backend)
    sell, buy = await _coin(host.coins, SELL), await _coin(host.coins, BUY)
    assert await host.set_pair(sell.address, buy.address)

    session = host.session
    result = session.quote(AMOUNT)
    plan = await session.plan_call(
        result, receiver=TRADER, sender=TRADER, slippage_bp=50.0, min_out_bp=50.0)
    assert not plan.reverted, f"a named budget would not go through: {plan.reverted}"

    contract = RouterContract(fork.provider(), TRADER)
    before = fork.erc20_balance(buy.address, TRADER)
    fork.wait(await contract.execute(plan))
    got = fork.erc20_balance(buy.address, TRADER) - before

    floor = int(result.route.modelled_out * (1 - 50.0 / 1e4))
    assert got >= floor, (
        f"produced {got} against a {floor} min_out the contract accepted"
    )
