"""Claiming what a gauge owes you, in CRV and in everything else."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Pool


@dataclass(frozen=True, slots=True)
class Rewards:
    """Where a chain's CRV lives, and who mints it."""

    #: The CRV token, for its logo and its symbol. 18 decimals everywhere.
    crv: str
    #: `mint(address)`: the Minter on Ethereum, a child gauge factory on
    #: every other chain.  A *fallback*: a gauge names its own minter and
    #: that is the answer -- see `minter_from_factory`.  Measured, not
    #: assumed: on six chains this named a factory that not one gauge on
    #: the chain uses, and every claim through it reverted.
    minter: str
    #: How many gauges `mint_many` takes at once.
    mint_many_size: int = 32
    #: Every child gauge factory known on this chain, newest last.  Asked
    #: `get_gauge_from_lp_token` when the pool list names a gauge that is
    #: not deployed here -- which it does for whole chains: every gauge the
    #: API lists for BSC and Sonic is the Ethereum *root* gauge.
    factories: tuple[str, ...] = ()


#: Keyed by chain id. See the module docstring for provenance.
REWARDS: dict[int, Rewards] = {
    1: Rewards(
        "0xD533a949740bb3306d119CC777fa900bA034cd52",
        "0xd061D61a4d941c39E5453435B6345Dc261C2fcE0",
        mint_many_size=8,
    ),  # Ethereum
    10: Rewards(
        "0x0994206dfE8De6Ec6920FF4D779B0d950605Fb53",
        "0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
        factories=("0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
                   "0x871fBD4E01012e2E8457346059e8C189d664DbA4"),
    ),  # Optimism
    56: Rewards(
        "0x9996D0276612d23b35f90C51EE935520B3d7355B",
        "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8",
        factories=("0xDb205f215f568ADf21b9573b62566f6d9a40bed6",
                   "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8"),
    ),  # BSC
    100: Rewards(
        "0x712b3d230f3c1c19db860d80619288b1f0bdd0bd",
        "0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
        factories=("0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
                   "0x7BE6BD57A319A7180f71552E58c9d32Da32b6f96"),
    ),  # Gnosis
    137: Rewards(
        "0x172370d5cd63279efa6d502dab29171933a610af",
        "0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
        factories=("0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
                   "0x55a1C26CE60490A15Bdd6bD73De4F6346525e01e"),
    ),  # Polygon
    146: Rewards(
        "0x5af79133999f7908953e94b7a5cf367740ebee35",
        "0xf3A431008396df8A8b2DF492C913706BDB0874ef",
    ),  # Sonic
    250: Rewards(
        "0x1E4F97b9f9F913c46F1632781732927B9019C68b",
        "0x004A476B5B76738E34c86C7144554B9d34402F13",
        factories=("0x004A476B5B76738E34c86C7144554B9d34402F13",),
    ),  # Fantom
    252: Rewards(
        "0x331B9182088e2A7d6D3Fe4742AbA1fB231aEcc56",
        "0xeF672bD94913CB6f1d2812a6e18c1fFdEd8eFf5c",
        factories=("0xeF672bD94913CB6f1d2812a6e18c1fFdEd8eFf5c",
                   "0x0B8D6B6CeFC7Aa1C2852442e518443B1b22e1C52"),
    ),  # Fraxtal
    324: Rewards(
        "0x5945932099f124194452a4c62d34bB37f16183B2",
        "0x167D9C27070Ce04b79820E6aaC0cF243d6098812",
    ),  # zkSync
    1284: Rewards(
        "0x7C598c96D02398d89FbCb9d41Eab3DF0C16F227D",
        "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8",
    ),  # Moonbeam
    8453: Rewards(
        "0x8Ee73c484A26e0A5df2Ee2a4960B789967dd0415",
        "0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
        factories=("0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
                   "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8"),
    ),  # Base
    42161: Rewards(
        "0x11cDb42B0EB46D95f990BeDD4695A6e3fA034978",
        "0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
        factories=("0xabC000d88f23Bb45525E447528DBF656A9D55bf5",
                   "0x988d1037e9608B21050A8EFba0c6C45e01A3Bce7"),
    ),  # Arbitrum
    42220: Rewards(
        "0x0a7432cF27F1aE3825c313F3C81e7D3efD7639aB",
        "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8",
        factories=("0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8",),
    ),  # Celo
    43114: Rewards(
        "0x47536F17F4fF30e64A96a7555826b8f9e66ec468",
        "0x97aDC08FA1D849D2C48C5dcC1DaB568B169b0267",
        factories=("0x97aDC08FA1D849D2C48C5dcC1DaB568B169b0267",),
    ),  # Avalanche
    1313161554: Rewards(
        "0x64D5BaF5ac030e2b7c435aDD967f787ae94D0205",
        "0xe35A879E5EfB4F1Bb7F70dCF3250f2e19f096bd8",
    ),  # Aurora
}

#: CRV is 18 decimals on every chain it exists on.
CRV_DECIMALS = 18


#: Child gauge factories a chain has retired but whose gauges still mint
#: through them.  Not a list this keeps: a gauge names its own factory and
#: that is the answer.  Here only to say why the table above is a fallback
#: and not the truth -- Arbitrum's entry is the v2 factory, which knows
#: nineteen gauges, while the one before it knows a hundred and forty-nine.
MINTER_IS_A_FALLBACK = True


def minter_from_factory(answer: str | None) -> str:
    """The minter a gauge's own `factory()` names, or "" if it did not say.

    A child gauge is minted through the factory that deployed it, and the
    address is immutable in the gauge: `assert msg.sender in [addr, FACTORY]`.
    So a chain with two factories has two independent minters, with separate
    `minted[user][gauge]` accounting and no lookup between them -- minting a
    pre-v2 gauge through the v2 factory fails on `gauge_data[gauge] == 0`,
    a bare assert with no reason string, which is why it reaches a wallet as
    an empty revert and an explorer shows nothing at all.
    """
    if not answer or len(answer) < 42:
        return ""
    found = "0x" + answer[-40:]
    return "" if int(found, 16) == 0 else found


def rewards_for(pool: Pool) -> Rewards | None:
    """Where to read and mint this pool's CRV, if CRV is minted here at all."""
    if not pool.has_gauge:
        return None
    return REWARDS.get(pool.chain_id)


def crv_token(pool: Pool) -> str:
    """CRV's address on this pool's chain, or "" if unknown."""
    entry = REWARDS.get(pool.chain_id)
    return entry.crv if entry else ""
