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

#: Sizes to move the market with, tried in turn until a plan priced before
#: them is refused.  Growing rather than fixed, because how far a trade moves
#: a pool is not something this can know in advance.
MARKET_MOVES = (100 * 10**18, 400 * 10**18, 1600 * 10**18, 6400 * 10**18)

#: What those moves ship with.  Wide on purpose: they are scaffolding and
#: must not revert on bounds of their own while putting the price where the
#: test needs it.
MOVE_SLIPPAGE_BP = 500.0

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

    # Against what the contract was actually given, not against a second
    # derivation of it -- and this test carried the very bug it was meant to
    # catch, taking its floor off the *model* where the model stood 50 bp
    # above what the route paid.
    assert got >= plan.guaranteed_out, (
        f"produced {got} against a {plan.guaranteed_out} min_out the contract "
        f"accepted"
    )
    # And the promise is about the number on screen: 50 bp under the quote,
    # which is what was asked for.
    under = (plan.quoted_out - plan.guaranteed_out) / plan.quoted_out * 1e4
    assert under == pytest.approx(50.0, abs=0.5), f"the bound sits {under:.2f} bp under"


async def test_a_plan_priced_before_the_market_moved_is_refused(
        fork, router_api, router_backend):
    """What the wallet was reporting: a plan built when the typing stopped,
    pressed a minute later, and refused at the head before it was signed.

    So the press has to price again.  Here the market is moved on purpose,
    which is what a minute of a volatile pair does on its own.

    **Staging that is not always possible, and where it is not this skips
    rather than fails.**  Which pools a trade goes through is the router's
    choice and it changes: after a facts rebuild, a hundred ether through
    this pair stopped touching the stale plan's pools, and 8,500 did not
    touch them either -- a large order goes by a different path than a tenth
    of one, so it was never a question of size.  None of that is a fault in
    the app and none of it should read as one.

    What stays an assertion is the subject: a plan priced before a move the
    chain *did* feel is refused, and re-pricing fixes it.
    """
    fork.give_eth(TRADER, 20_000)
    host = await _warmed(fork, router_api, router_backend)
    sell, buy = await _coin(host.coins, SELL), await _coin(host.coins, BUY)
    assert await host.set_pair(sell.address, buy.address)
    session = host.session

    stale_result = session.quote(AMOUNT)
    stale_route = stale_result.route
    stale = await session.plan_call(
        stale_result, receiver=TRADER, sender=TRADER,
        slippage_bp=None, min_out_bp=0.0)
    assert not stale.reverted, "the plan was refused before anything moved"

    # Moved through the first *priced* leg's own pair.  Not the route's,
    # because a large order does not take the small one's path; and not the
    # first leg, which is the ETH -> WETH wrap, where any size moves nothing
    # because wrapping is one for one.
    leg = next(one for one in stale_route.legs
               if not one.is_conversion and one.amount_in and one.amount_out)
    # Sold from the *native* side, not from the leg's own input token.  The
    # trader holds ether and nothing else: pointing this at WETH -> crvUSD
    # made every move revert on the `transferFrom`, having neither the token
    # nor an allowance, which reads as a market that would not move and is
    # nothing of the sort.  Buying the leg's output with ether wraps on the
    # way and arrives at the same pool.
    if not await host.set_pair(sell.address, leg.token_out):
        pytest.skip(f"could not prepare {sell.symbol} -> {leg.token_out}")

    contract = RouterContract(fork.provider(), TRADER)
    moved: list[float] = []
    refused: list[str] = []
    for size in MARKET_MOVES:
        # Bounded wide, and the state re-read after each: these exist to move
        # the price, not to be protected from it, and under the automatic rule
        # each is priced against pools the one before it has already moved.
        big = await session.plan_call(
            session.quote(size), receiver=TRADER, sender=TRADER,
            slippage_bp=MOVE_SLIPPAGE_BP, min_out_bp=0.0)
        try:
            fork.wait(await contract.execute(big))
        except AssertionError as exc:
            # One size refusing is not the end of it -- the next may go
            # through, and giving up on the first was skipping runs that
            # could still have staged the move.
            refused.append(f"{size / 10**18:,.0f} ({str(exc).splitlines()[0]})")
            continue
        moved.append(size / 10**18)
        if _refused(fork, stale):
            break
        await host.after_swap(int(fork.rpc("eth_blockNumber"), 16))
    else:
        pytest.skip(
            f"could not stage a move into {leg.token_out}: "
            f"{sum(moved):,.0f} went through and did not shift it enough"
            + (f"; refused outright: {', '.join(refused)}" if refused else "")
        )

    # Back to the pair under test before re-pricing it.
    assert await host.set_pair(sell.address, buy.address)
    await host.after_swap(int(fork.rpc("eth_blockNumber"), 16))
    fresh = await session.plan_call(
        session.quote(AMOUNT), receiver=TRADER, sender=TRADER,
        slippage_bp=None, min_out_bp=0.0)
    assert not fresh.reverted, f"re-pricing did not help: {fresh.reverted}"
    assert not _refused(fork, fresh), "the wallet would still have refused it"


def _refused(fork, plan) -> bool:
    """Whether the chain refuses this exact call, as a wallet would find out."""
    payload = {"from": TRADER, "to": plan.to,
               "value": hex(int(plan.value)), "data": "0x" + plan.data.hex()}
    try:
        fork.rpc("eth_call", [payload, "latest"])
    except AssertionError:
        return True
    return False
