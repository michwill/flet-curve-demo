"""Curve's deposit-and-stake zap: both halves in one transaction.

Depositing and then staking is two transactions, and between them sits an
LP token the depositor never wanted to hold -- and a second wallet prompt
that a distracted person can simply not answer, leaving liquidity earning
nothing. Curve deploys one contract per chain that does both: it pulls the
coins, deposits them into the pool (or through the pool's own deposit zap),
and puts the minted LP straight into the gauge.

**Two signatures, and the difference is invisible until it reverts.**

    deposit_and_stake(address,address,address,uint256,address[],uint256[],
                      uint256,bool,bool,address)   -- ten arguments
    deposit_and_stake(address,address,address,uint256,address[],uint256[],
                      uint256,bool,address)        -- nine

The ten-argument form carries `use_underlying` before `use_dynarray`; the
nine-argument form has no such flag. Which one a chain speaks is not a
property of the contract's age but of the *pools* on that chain: the flag
exists for lending pools and old crypto metapools, and a chain deployed
after those stopped being made has no use for it. `OLD_CHAINS` below is
curve-js's own list, and a wrong guess is a selector for a function that
does not exist -- calldata that reverts and costs gas.

**Where these came from.** The addresses are the `deposit_and_stake` alias
in curve-js's `src/constants/network_constants.ts`, which is Curve's own
published mapping, and the two ABIs are its
`src/constants/abis/deposit_and_stake*.json`. Every address here was then
checked with `eth_getCode` against a public node for that chain -- all
sixteen hold code, and each chain id was cross-checked against chainlist's
name for it so a transposed id would show up as a mismatch rather than as
an empty read.

That is a weaker check than `curve.zaps` got: those were each quoted with a
real `eth_call`, and `deposit_and_stake` has no view function to quote --
it only writes. So the route is offered on the strength of the deposit
quote the panel already makes through the pool, and the zap itself is not
exercised until the transaction is sent.

**Two limitations worth naming**, both inherited rather than introduced:

  * the oldest gauges gate deposits made on someone's behalf behind
    `set_approve_deposit`, a second approval this app does not send. Those
    gauges are pre-factory and rare; a deposit-and-stake into one reverts,
    and the separate Deposit then Stake path still works.
  * a pool whose coin is native ETH needs the value sent with the call.
    Nothing in this app sends value with a transaction -- see
    `PoolContract._send` -- so those pools cannot deposit here at all,
    with or without staking. This changes nothing about that.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Pool

#: The nowhere address, for arguments that are "not applicable".
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

#: Chains whose zap takes the ten-argument form, with `use_underlying`.
#: Transcribed from curve-js's `OLD_CHAINS`, which is defined there as
#: "these chains have non-ng pools" -- the pools the flag exists for.
#: Moonbeam (1284), Celo (42220) and Aurora (1313161554) are on that list
#: and have no zap address published, so they never reach this table.
OLD_CHAINS = frozenset({1, 10, 56, 100, 137, 250, 1284, 2222, 8453, 42161, 42220, 43114})


@dataclass(frozen=True, slots=True)
class StakeZap:
    """The deposit-and-stake contract for one chain."""

    address: str
    #: Does it take `use_underlying`? See the module docstring: this is the
    #: ten-argument form against the nine-argument one.
    use_underlying_arg: bool


#: Keyed by chain id. Aurora publishes the zero address -- no zap deployed
#: -- and is left out rather than stored as a zero somebody has to
#: remember to check.
STAKE_ZAPS: dict[int, StakeZap] = {
    1: StakeZap("0x56C526b0159a258887e0d79ec3a80dfb940d0cD7", True),  # Ethereum
    10: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Optimism
    56: StakeZap("0x4f37A9d177470499A2dD084621020b023fcffc1F", True),  # BSC
    100: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Gnosis
    137: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Polygon
    146: StakeZap("0x505d666E4DD174DcDD7FA090ed95554486d2Be44", False),  # Sonic
    196: StakeZap("0x5552b631e2aD801fAa129Aacf4B701071cC9D1f7", False),  # X Layer
    250: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Fantom
    252: StakeZap("0xF0d4c12A5768D806021F80a262B4d39d26C58b8D", False),  # Fraxtal
    324: StakeZap("0x253548e98C769aD2850da8DB3E4c2b2cE46E3839", False),  # zkSync
    999: StakeZap("0x5a8C93EE12a8Df4455BA111647AdA41f29D5CfcC", False),  # HyperEVM
    2222: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Kava
    5000: StakeZap("0xF0d4c12A5768D806021F80a262B4d39d26C58b8D", False),  # Mantle
    8453: StakeZap("0x69522fb5337663d3B4dFB0030b881c1A750Adb4f", True),  # Base
    42161: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Arbitrum
    43114: StakeZap("0x37c5ab57AF7100Bdc9B668d766e193CCbF6614FD", True),  # Avalanche
}


def stake_zap_for(pool: Pool) -> StakeZap | None:
    """The deposit-and-stake contract for this pool, if the route exists.

    None when the pool has no gauge -- there is nothing to stake into, so
    the combined call has no meaning -- or when the chain has no zap
    deployed. Both are ordinary answers: the caller falls back to
    depositing and staking as two transactions, which works everywhere.
    """
    if not pool.has_gauge or not pool.lp_token:
        return None
    return STAKE_ZAPS.get(pool.chain_id)


def consistent_variants() -> bool:
    """Every entry's flag agrees with `OLD_CHAINS`. Asserted by a test.

    The two facts are kept separately -- one is a per-chain address, the
    other a property of a whole generation of pools -- and this is what
    stops them drifting apart when a chain is added.
    """
    return all(
        zap.use_underlying_arg == (chain_id in OLD_CHAINS)
        for chain_id, zap in STAKE_ZAPS.items()
    )
