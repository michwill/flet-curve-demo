"""Which deposit zap serves which metapool, and in which ABI dialect.

A metapool holds two coins: its own, and the *LP token of a base pool*. So
the World Liberty pool is USD1 paired with crv2pool, and depositing USDC
into it means minting crv2pool first -- a second transaction, a second
approval, and a token nobody wanted to hold.

A zap does both in one call. It takes the **underlying** list instead --
the metapool's own coin followed by the base pool's coins, which is exactly
`Pool.display_coins` -- and every function carries the pool address as its
first argument, because one zap serves every metapool built on the same
base.

Two dialects exist, and they are the same split as the pools themselves:

  * StableSwap-NG's zap takes `uint256[]` (a Vyper `DynArray`);
  * the older factory zaps take `uint256[N]`, N fixed by the base pool.

Which one a metapool needs follows from its own implementation, so
`zap_for` keys off `Pool.dynamic_arrays` rather than asking separately.

**Where these addresses came from.** Curve's own API publishes a
`zapAddress` per pool (`api.curve.finance/api/getPools/{chain}/{registry}`),
which the Prices v2 API this app otherwise uses does not carry. They are
transcribed here rather than fetched, because a zap is a property of a
factory and a base pool -- one address per base pool, not per pool -- and
because a second API call on every pool page to learn a constant is a poor
trade. `coins` is the length that base pool's zap expects; a metapool whose
decomposed coin list does not match it is not offered the underlying route
at all.

Two families are deliberately absent. The oldest `main`-registry metapools
(GUSD/3Crv and friends) have a *per-pool* deposit contract with no pool
argument, which is a different interface for a dozen pools. And several
NG metapools have no zap published at all, on Ethereum and on Arbitrum.
Both simply keep the pool-token route, which works everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Pool

#: Pool types this applies to. The zaps below are *factory* zaps: one per
#: base pool, addressed by pool. The oldest metapools are not from a
#: factory at all -- they predate it, their LP token is a separate contract
#: from the pool, and each has its own deposit contract with no pool
#: argument. Handing one of those to a factory zap is calldata that at best
#: reverts, so they are excluded by type rather than by address.
FACTORY_METAPOOL_TYPES = frozenset(
    {"factory", "stableswapng", "factory-stable-ng"}
)


@dataclass(frozen=True, slots=True)
class Zap:
    """One deposit zap, as it applies to metapools on a given base pool."""

    address: str
    #: Underlying coins it expects: 1 + the base pool's own count. Part of
    #: the signature in the fixed dialect, and merely required in the
    #: dynamic one -- the NG zap reverts on a wrong length either way.
    coins: int
    #: `uint256[]` rather than `uint256[N]`.
    dynamic: bool


#: Keyed by `(chain_id, base pool address lowercased, dynamic)`. The last
#: element matters on Ethereum, where 3pool and FraxBP each have both an
#: old factory zap and the NG one.
#:
#: Transcribed from `zapAddress` across all 21 chains and both stable
#: registries of `api.curve.finance` -- see the module docstring -- and then
#: checked one by one with an `eth_call` to each zap on its own chain,
#: quoting a real deposit into its busiest metapool. Sixteen of the
#: twenty-one answered; the five marked below revert on every sample pool
#: they serve, which are small or empty deployments. Those are kept because
#: the data is Curve's own and the route is gated on a working quote
#: anyway: a zap that will not answer offers no approve step and the
#: pool-token route stays.
ZAPS: dict[tuple[int, str, bool], Zap] = {
    # -- Ethereum --------------------------------------------------
    (1, "0x4f493b7de8aac7d55f71853688b1f7c8f0243c85", True): Zap(
        "0xE07a16358aA878CBDa2D49A88E5106871E0db307", 3, True
    ),  # Strategic USD Reserves
    (1, "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7", True): Zap(
        "0xE07a16358aA878CBDa2D49A88E5106871E0db307", 4, True
    ),  # 3pool -- unverified, see above
    (1, "0xdcef968d416a41cdac0ed8702fac8128a64241a2", True): Zap(
        "0xE07a16358aA878CBDa2D49A88E5106871E0db307", 3, True
    ),  # FraxBP -- unverified, see above
    (1, "0x383e6b4437b59fff47b619cba855ca29342a8559", True): Zap(
        "0xE07a16358aA878CBDa2D49A88E5106871E0db307", 3, True
    ),
    (1, "0xa5588f7cdf560811710a2d82d3c9c99769db1dcb", True): Zap(
        "0xE07a16358aA878CBDa2D49A88E5106871E0db307", 3, True
    ),
    (1, "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7", False): Zap(
        "0xA79828DF1850E8a3A3064576f380D90aECDD3359", 4, False
    ),  # 3pool
    (1, "0xdcef968d416a41cdac0ed8702fac8128a64241a2", False): Zap(
        "0x08780fb7E580e492c1935bEe4fA5920b94AA95Da", 3, False
    ),  # FraxBP
    (1, "0x7fc77b5c7614e1533320ea6ddc2eb61fa00a9714", False): Zap(
        "0x7AbDBAf29929e7F8621B757D2a7c04d78d633834", 4, False
    ),  # sBTC
    (1, "0xf253f83aca21aabd2a20553ae0bf7f65c755a07f", False): Zap(
        "0xA2d40Edbf76C6C0701BA8899e2d059798eBa628e", 3, False
    ),  # sBTC v2
    # -- Optimism --------------------------------------------------
    (10, "0x1337bedc9d22ecbe766df105c9623922a27963ec", False): Zap(
        "0x167e42a1C7ab4Be03764A2222aAC57F5f6754411", 4, False
    ),  # 3pool
    (10, "0x29a3d66b30bc4ad674a4fdaf27578b64f6afbfe7", False): Zap(
        "0x4244eB811D6e0Ef302326675207A95113dB4E1F8", 3, False
    ),
    # -- Gnosis ----------------------------------------------------
    (100, "0x7f90122bf0700f9e7e1f688fe926940e8839f353", False): Zap(
        "0x87C067fAc25f123554a0E76596BF28cFa37fD5E9", 4, False
    ),  # 2pool
    # -- Polygon ---------------------------------------------------
    (137, "0x445fe580ef8d70ff569ab36e80c647af338db351", False): Zap(
        "0x5ab5C56B9db92Ba45a0B46a207286cD83C15C939", 4, False
    ),  # aave 3pool
    (137, "0xc2d95eef97ec6c17551d45e77b590dc1f9117c67", False): Zap(
        "0xE2e6DC1708337A6e59f227921db08F21e3394723", 3, False
    ),  # unverified, see above
    # -- Fantom ----------------------------------------------------
    (250, "0x27e611fd27b276acbd5ffd632e5eaebec9761e40", False): Zap(
        "0x78D51EB71a62c081550EfcC0a9F9Ea94B2Ef081c", 3, False
    ),  # 2pool
    (250, "0x3ef6a01a0f81d6046290f3e2a8c5b843e738e604", False): Zap(
        "0x001E3BA199B4FF4B5B6e97aCD96daFC0E2e4156e", 3, False
    ),  # unverified, see above
    (250, "0x0fa949783947bf6c1b171db13aeacbb488845b3f", False): Zap(
        "0x247aEB220E87f24c40C9F86b65d6bd5d3c987B55", 4, False
    ),
    # -- Arbitrum --------------------------------------------------
    (42161, "0x7f90122bf0700f9e7e1f688fe926940e8839f353", False): Zap(
        "0x7544Fe3d184b6B55D6B36c3FCA1157eE0Ba30287", 3, False
    ),  # 2pool
    (42161, "0xc9b8a3fdecb9d5b218d02555a8baf332e5b740d5", False): Zap(
        "0x58AC91f5BE7dC0c35b24B96B19BAc55FBB8E705e", 3, False
    ),
    (42161, "0x3e01dd8a5e1fb3481f0f589056b428fc308af0fb", False): Zap(
        "0x803A2B40c5a9BB2B86DD630B274Fa2A9202874C2", 3, False
    ),  # unverified, see above
    # -- Avalanche -------------------------------------------------
    (43114, "0x7f90122bf0700f9e7e1f688fe926940e8839f353", False): Zap(
        "0x001E3BA199B4FF4B5B6e97aCD96daFC0E2e4156e", 4, False
    ),  # 2pool
}


def zap_for(pool: Pool) -> Zap | None:
    """The zap that can deposit this metapool's underlying coins, if any.

    None whenever anything does not line up -- not a metapool, no zap known
    for that base pool on that chain, or a decomposed coin list whose length
    disagrees with the zap's. The last case is the one worth being strict
    about: the fixed dialect encodes N in the signature, so a mismatch is
    calldata for a function that does not exist, and in the dynamic dialect
    it is a revert inside the zap. Either way the pool-token route still
    works, which is what the caller falls back to.
    """
    if not pool.base_pool:
        return None
    if (pool.registry or "").lower() not in FACTORY_METAPOOL_TYPES:
        return None
    key = (pool.chain_id, pool.base_pool.lower(), pool.dynamic_arrays)
    zap = ZAPS.get(key)
    if zap is None:
        return None
    if len(pool.display_coins) != zap.coins:
        return None
    return zap
