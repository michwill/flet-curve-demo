"""What a transaction will cost, and the three ways of getting it wrong.

The arithmetic is small; the traps are not. A wallet asks for headroom it
does not spend -- a gas limit half again over the estimate, a fee ceiling
at twice the base fee -- and quoting either would overstate every fee on
the page. And not every chain settles in ether, which is a factor of a
few thousand rather than a rounding error.

The last test here checks the model against qeth's own `apply_gas_policy`
rather than against a transcription of it, because a policy copied by hand
is a policy that drifts.
"""

from __future__ import annotations

import ast
import random
import types
from pathlib import Path

import pytest

from curve.gas import (
    LEGACY_DENOMINATOR,
    LEGACY_NUMERATOR,
    MIN_TIP_WEI,
    NATIVE,
    NATIVE_PSEUDO,
    fee_in_native,
    format_fee,
    native_for,
    native_price,
    settlement_price,
)

GWEI = 10**9


# -- what it settles at ------------------------------------------------------


def test_the_tip_is_five_per_cent_over_the_base_fee() -> None:
    assert settlement_price(base_fee=30 * GWEI, gas_price=31 * GWEI) == (
        30 * GWEI + 30 * GWEI * 5 // 100
    )


def test_the_ceiling_the_wallet_asks_for_is_not_the_price() -> None:
    """qeth names twice the base fee as `maxFeePerGas` so a spike still
    lands. The difference is refunded, and quoting it would double every
    fee on the page."""
    base = 30 * GWEI
    assert settlement_price(base_fee=base, gas_price=base) < base * 2


def test_a_node_tip_above_five_per_cent_wins() -> None:
    """OP-stack L2s: the base fee is a few hundred wei, so five per cent
    of it underflows and the node's own suggestion is the real floor."""
    assert settlement_price(base_fee=1_500, gas_price=1_600, node_tip=10**6) == (
        1_500 + 10**6
    )


def test_a_chain_too_idle_to_have_a_tip_still_pays_one() -> None:
    """Gnosis bounces a transaction whose priority fee is under a wei, and
    five per cent of its base fee floors to nothing."""
    assert settlement_price(base_fee=5, gas_price=10) == 5 + MIN_TIP_WEI


def test_a_chain_with_no_base_fee_pays_the_reported_price() -> None:
    """BNB Smart Chain: `baseFee` is zero and the gas price *is* the
    mandatory tip, so the whole cost is that tip -- not twice it, which
    is only the ceiling."""
    assert settlement_price(base_fee=0, gas_price=3 * GWEI) == 3 * GWEI


def test_a_legacy_chain_pays_what_it_names() -> None:
    """There is no refund of the difference on a legacy transaction, so
    the wallet's markup is part of the cost rather than headroom."""
    assert settlement_price(base_fee=0, gas_price=5 * GWEI, eip1559=False) == (
        5 * GWEI * LEGACY_NUMERATOR // LEGACY_DENOMINATOR
    )


def test_the_five_per_cent_is_integer_arithmetic() -> None:
    """A base fee is large enough that a float round-trip lands a wei
    either side of what the wallet computed."""
    base = 10**18 + 7
    assert settlement_price(base_fee=base, gas_price=base) == (
        base + base * 5 // 100
    )


# -- whose coin --------------------------------------------------------------


def test_three_of_the_supported_chains_do_not_run_on_ether() -> None:
    """XDAI is a dollar and POL a fraction of one. A fee labelled ETH on
    Gnosis is wrong by three orders of magnitude."""
    assert native_for(100).symbol == "XDAI"
    assert native_for(137).symbol == "POL"
    assert native_for(56).symbol == "BNB"
    assert native_for(1).symbol == "ETH"


def test_an_unlisted_chain_is_assumed_to_settle_in_ether() -> None:
    """Which is true of every EVM rollup this app can reach. A wrong
    symbol there costs a label; refusing to price anything unlisted would
    cost the line on chains where it is right."""
    assert native_for(1_234_567).symbol == "ETH"
    assert native_for(1_234_567).wrapped == ""


class Prices:
    """A price endpoint that answers for some addresses and not others."""

    def __init__(self, known: dict[str, float]) -> None:
        self.known = {k.lower(): v for k, v in known.items()}
        self.asked: list[str] = []

    async def usd_price(self, _chain: str, address: str) -> float:
        self.asked.append(address.lower())
        return self.known.get(address.lower(), 0.0)


async def test_the_coin_itself_is_asked_for_before_its_wrapper() -> None:
    """On Polygon the pseudo-address prices POL at $0.54 and the wrapper
    reads $0.08 -- the wrong way round is sevenfold wrong on a live
    chain, so the order is the test."""
    api = Prices({NATIVE_PSEUDO: 0.54, NATIVE[137].wrapped: 0.08})

    assert await native_price(api, "polygon", 137) == 0.54
    assert api.asked == [NATIVE_PSEUDO.lower()], "the wrapper is not even asked"


async def test_the_wrapper_answers_where_the_coin_will_not() -> None:
    """Base, Gnosis and Fraxtal return nothing for the pseudo-address."""
    api = Prices({NATIVE[8453].wrapped: 1_880.94})

    assert await native_price(api, "base", 8453) == 1_880.94


async def test_a_chain_nobody_prices_gets_no_number_rather_than_a_wrong_one() -> None:
    api = Prices({})
    assert await native_price(api, "somechain", 999_999) == 0.0


# -- how it reads ------------------------------------------------------------


def test_a_fee_worth_naming_is_given_in_cents() -> None:
    assert format_fee(0.00042, "ETH", 0.79) == "0.00042 ETH  ($0.79)"


def test_a_fee_far_under_a_cent_is_not_rounded_to_nothing() -> None:
    """An L2 fee is often a thousandth of a cent, and `$0.00` on every one
    of them makes the line useless exactly where it reassures most."""
    assert format_fee(0.0000004, "ETH", 0.00000075) == (
        "0.0000004 ETH  ($0.00000075)"
    )


def test_an_unpriced_chain_still_says_what_the_fee_is() -> None:
    assert format_fee(0.0021, "XDAI", 0.0) == "0.0021 XDAI"


def test_the_fee_is_gas_times_price() -> None:
    assert fee_in_native(21_000, 31_500_000_000) == pytest.approx(0.0006615)


# -- against the wallet's own code -------------------------------------------

QETH = Path.home() / "Projects/qeth/qeth/plugins/transactions/__init__.py"


def qeth_policy():
    """`apply_gas_policy`, lifted out of qeth without importing it.

    The module pulls in PySide6 at import time, so the one function is
    compiled on its own. That is the point of the exercise: this runs
    *their* code, not a copy of it that can quietly drift from theirs.
    """
    tree = ast.parse(QETH.read_text())
    picked = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "apply_gas_policy"
    ]
    if not picked:
        pytest.skip("qeth no longer defines apply_gas_policy at module level")
    module = types.ModuleType("qeth_policy")
    module.__dict__["_MIN_PRIORITY_FEE_WEI"] = MIN_TIP_WEI
    # Evaluated at def time; the body only reads attributes off it.
    module.__dict__["SigningRequest"] = object
    exec(
        compile(ast.Module(body=picked, type_ignores=[]), "<qeth>", "exec"),
        module.__dict__,
    )
    return module.apply_gas_policy


class Silent:
    """A dapp that names no gas and no fee, which is what this app sends."""

    gas = None
    max_fee_per_gas = None
    gas_price = None


@pytest.mark.skipif(not QETH.is_file(), reason="qeth is not checked out here")
def test_the_model_agrees_with_the_wallet_that_will_pay_it() -> None:
    """Two hundred random chain states, plus the six that have names.

    Settlement is the EIP's own rule -- `min(maxFee, baseFee + tip)` --
    rather than anything qeth reports, because in its zero-base-fee branch
    it stores the reference gas price under `base_fee` and the chain's
    actual base fee is still zero. Reading that key as the base fee is
    what made this disagree on BSC the first time it ran.
    """
    apply_gas_policy = qeth_policy()

    def theirs(base: int, price: int, tip: int, eip1559: bool) -> int:
        out = apply_gas_policy(
            estimated_gas=200_000,
            eip1559=eip1559,
            base_fee_wei=base,
            gas_price_wei=price,
            req=Silent(),
            max_priority_fee_wei=tip,
        )
        if not eip1559:
            return out["gas_price"]
        return min(out["max_fee_per_gas"], base + out["max_priority_fee_per_gas"])

    random.seed(7)
    cases = [
        (30 * GWEI, 31 * GWEI, GWEI, True),          # Ethereum, busy
        (120_000_000, 130_000_000, 10**6, True),     # Ethereum, idle
        (1_500, 1_600, 10**6, True),                 # OP-stack L2
        (5, 10, 0, True),                            # Gnosis, idle
        (0, 3 * GWEI, 0, True),                      # BSC-style
        (0, 5 * GWEI, 0, False),                     # legacy
    ]
    cases += [
        (
            random.randrange(0, 10**11),
            random.randrange(1, 10**11),
            random.randrange(0, 10**9),
            random.random() > 0.2,
        )
        for _ in range(200)
    ]

    for base, price, tip, eip1559 in cases:
        assert settlement_price(
            base_fee=base, gas_price=price, node_tip=tip, eip1559=eip1559
        ) == theirs(base, price, tip, eip1559), (
            f"baseFee={base} gasPrice={price} tip={tip} eip1559={eip1559}"
        )
