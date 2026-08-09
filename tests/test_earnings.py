"""Boost, the rate it implies, and how many transactions a claim takes.

The arithmetic is the whole feature. Curve pays CRV on a *working*
balance, so the published APR is nobody's actual rate: an account with no
veCRV earns on 40% of its deposit and a fully boosted one on all of it,
which is the 1x to 2.5x spread. Getting that wrong shows somebody a number
they cannot earn.

The second half is the claim, and it is a counting problem rather than a
maths one. CRV batches -- `mint_many` takes eight gauges on Ethereum and
thirty-two elsewhere -- and the incentive tokens do not batch at all, so
"claim everything" is not one transaction and the page must not say it is.
"""

from __future__ import annotations

import pytest

from curve.earnings import (
    MAX_BOOST,
    ClaimPlan,
    Earning,
    Reward,
    claim_plan,
    seed_from_detail,
)

CRV = Reward("", "CRV", 18, 5 * 10**18, 0.5)
ARB = Reward("0x" + "ab" * 20, "ARB", 18, 2 * 10**18, 1.5)
USDC = Reward("0x" + "cd" * 20, "USDC", 6, 2_500_000, 1.0)


def staked(**kw) -> Earning:
    base = {"pool": "0xpool", "gauge": "0xgauge", "staked": 1000, "working": 400}
    return Earning(**{**base, **kw})


# -- boost ------------------------------------------------------------------


def test_no_ve_crv_is_the_floor_not_a_penalty() -> None:
    """`working = 0.4 * balance` is what everyone gets for free: 1x."""
    assert staked(working=400).boost == 1.0


def test_a_full_working_balance_is_the_ceiling() -> None:
    assert staked(working=1000).boost == MAX_BOOST == 2.5


def test_the_boost_is_clamped_to_what_the_protocol_allows() -> None:
    """A gauge mid-checkpoint can report more than the deposit briefly."""
    assert staked(working=99999).boost == 2.5


def test_an_unstaked_position_has_no_boost_rather_than_the_minimum() -> None:
    """0.0, not 1.0. It earns no CRV at all, and "1.00x" says otherwise."""
    assert Earning(pool="p", gauge="g", staked=0, wallet=500).boost == 0.0


def test_a_gauge_that_did_not_answer_reads_as_no_boost() -> None:
    assert staked(working=0).boost == 0.0


# -- the rate that follows --------------------------------------------------


def test_the_rate_is_the_published_one_times_the_boost() -> None:
    assert staked(working=1000, crv_apr=10.0).user_crv_apr == 25.0


def test_rewards_are_paid_on_the_staked_part_only() -> None:
    """One LP staked and nine loose earns a tenth of the headline rate.

    The published APR is a property of the gauge; what an account earns is
    a property of its position, and most of this one is not in the gauge.
    """
    position = Earning(
        pool="p", gauge="g", staked=100, wallet=900, working=100, crv_apr=10.0
    )
    assert position.staked_share == pytest.approx(0.1)
    assert position.user_crv_apr == pytest.approx(2.5)  # 10 * 2.5 boost * 0.1


def test_incentives_are_staked_only_but_never_boosted() -> None:
    """Boost is a CRV mechanism; a reward token is streamed pro rata."""
    position = Earning(
        pool="p", gauge="g", staked=500, wallet=500, working=500, incentive_apr=4.0
    )
    assert position.boost == 2.5
    assert position.user_incentive_apr == pytest.approx(2.0)


def test_the_two_rates_add_up() -> None:
    position = Earning(
        pool="p", gauge="g", staked=1000, working=400,
        crv_apr=6.0, incentive_apr=4.0,
    )
    assert position.user_apr == pytest.approx(10.0)


# -- what is owed -----------------------------------------------------------


def test_a_reward_is_valued_in_its_own_decimals() -> None:
    """2.5 USDC at $1 is $2.50, not $2.5e-12."""
    assert USDC.whole == pytest.approx(2.5)
    assert USDC.value == pytest.approx(2.5)


def test_the_value_owed_is_every_token_together() -> None:
    position = staked(rewards=(CRV, ARB, USDC))
    assert position.claimable_value == pytest.approx(5 * 0.5 + 2 * 1.5 + 2.5)


def test_a_token_with_no_price_still_counts_as_owed() -> None:
    """Unpriced is not worthless -- the claim button must still appear."""
    position = staked(rewards=(Reward("0xtok", "MYSTERY", 18, 10**18, 0.0),))
    assert position.claimable_value == 0.0
    assert position.has_extras is True


def test_crv_and_extras_are_told_apart() -> None:
    assert staked(rewards=(CRV,)).has_crv is True
    assert staked(rewards=(CRV,)).has_extras is False
    assert staked(rewards=(ARB,)).has_crv is False
    assert staked(rewards=(ARB,)).has_extras is True


# -- the claim ---------------------------------------------------------------


def test_crv_batches_eight_at_a_time_on_ethereum() -> None:
    """`mint_many(address[8])`, so ten gauges is two transactions."""
    many = [
        Earning(pool=f"p{i}", gauge=f"0x{i:040x}", staked=1, rewards=(CRV,))
        for i in range(10)
    ]
    plan = claim_plan(1, many)
    assert [len(gauges) for _minter, gauges in plan.crv] == [8, 2]
    assert plan.crv[0][0] == "0xd061D61a4d941c39E5453435B6345Dc261C2fcE0"


def test_crv_batches_thirty_two_at_a_time_elsewhere() -> None:
    """The child gauge factories declare `address[32]`."""
    many = [
        Earning(pool=f"p{i}", gauge=f"0x{i:040x}", staked=1, rewards=(CRV,))
        for i in range(10)
    ]
    assert [len(g) for _m, g in claim_plan(42161, many).crv] == [10]


def test_incentives_do_not_batch_at_all() -> None:
    """One `claim_rewards()` per gauge. There is no batching contract, and
    the page says the count rather than implying one prompt."""
    many = [
        Earning(pool=f"p{i}", gauge=f"0x{i:040x}", staked=1, rewards=(ARB,))
        for i in range(3)
    ]
    plan = claim_plan(1, many)
    assert plan.crv == ()
    assert len(plan.extras) == 3
    assert plan.transactions == 3


def test_a_gauge_owing_nothing_is_left_out_of_the_plan() -> None:
    positions = [
        Earning(pool="a", gauge="0x" + "11" * 20, staked=1, rewards=(CRV,)),
        Earning(pool="b", gauge="0x" + "22" * 20, staked=1),
    ]
    plan = claim_plan(1, positions)
    assert plan.crv[0][1] == ("0x" + "11" * 20,)
    assert plan.extras == ()


def test_a_chain_with_no_crv_offers_no_crv_claim() -> None:
    """X Layer and friends: the incentive half still works."""
    positions = [Earning(pool="a", gauge="0xg", staked=1, rewards=(CRV, ARB))]
    plan = claim_plan(196, positions)
    assert plan.crv == ()
    assert plan.extras == ("0xg",)


def test_an_empty_plan_is_no_transactions() -> None:
    assert ClaimPlan().transactions == 0
    assert claim_plan(1, []).transactions == 0


# -- seeding from the pool payload -------------------------------------------


def test_the_published_rates_are_taken_off_the_payload() -> None:
    seeded, meta = seed_from_detail(
        staked(),
        {
            "crv_apr": 3.5,
            "crv_apr_boosted": 8.75,
            "extra_rewards_apr": [
                {"address": "0xAA", "symbol": "ARB", "decimals": 18,
                 "price": 1.5, "apr": 4.0},
                {"address": "0xBB", "symbol": "USDC", "decimals": 6,
                 "price": 1.0, "apr": 1.0},
            ],
        },
    )
    assert seeded.crv_apr == 3.5
    assert seeded.crv_apr_max == 8.75
    assert seeded.incentive_apr == pytest.approx(5.0)
    assert meta["0xaa"] == ("ARB", 18, 1.5)
    assert meta["0xbb"] == ("USDC", 6, 1.0)


def test_crv_in_the_reward_list_is_not_counted_twice() -> None:
    """Some pools report CRV as an extra reward as well as in `crv_apr`.

    `Pool.from_v2` drops it there for the same reason; adding it here too
    would state the CRV rate twice and call the total an APR.
    """
    seeded, meta = seed_from_detail(
        staked(),
        {
            "crv_apr": 3.0,
            "extra_rewards_apr": [
                {"address": "0xCRV", "symbol": "CRV", "price": 0.5, "apr": 9.0},
                {"address": "0xAA", "symbol": "ARB", "price": 1.5, "apr": 4.0},
            ],
        },
    )
    assert seeded.incentive_apr == pytest.approx(4.0)
    assert "0xcrv" not in meta


def test_a_payload_with_no_rewards_seeds_cleanly() -> None:
    seeded, meta = seed_from_detail(staked(), {})
    assert (seeded.crv_apr, seeded.incentive_apr, meta) == (0.0, 0.0, {})
