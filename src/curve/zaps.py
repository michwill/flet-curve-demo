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

**Four dialects**, and none of them can be inferred from the others --
each was probed against deployed contracts:

  * StableSwap-NG's zap: pool argument, `uint256[]` (a Vyper `DynArray`),
    `is_deposit` flag, `int128` indices;
  * the older stable factory zaps: pool argument, `uint256[N]`, flag,
    `int128`;
  * the crypto factory's zaps: pool argument, `uint256[N]`, **no flag**,
    `uint256` indices;
  * the older crypto metapools' zaps: **no pool argument at all** -- one
    zap was deployed per pool -- so the calldata is exactly what the pool
    itself would take.

Which is why there are three tables below rather than one. They differ in
what addresses a zap, not just in what it speaks: by base pool for the
factory zaps, by pool for the ones deployed per pool.

**Where these addresses came from.** Curve's own API publishes a
`zapAddress` per pool (`api.curve.finance/api/getPools/{chain}/{registry}`),
which the Prices v2 API this app otherwise uses does not carry. They are
transcribed here rather than fetched, because a second API call on every
pool page to learn a constant is a poor trade. The sweep covers all 21
chains and every registry, and each entry was then checked with an
`eth_call` to that zap on its own chain. `coins` is the length the zap
expects; a metapool whose decomposed coin list does not match it is not
offered the underlying route at all.

One family is still absent: the oldest `main`-registry metapools
(GUSD/3Crv and friends), whose per-pool deposit contracts did not answer
any spelling probed against them. Several NG metapools have no zap
published at all either, on Ethereum, Gnosis and Arbitrum. Both keep the
pool-token route, which works everywhere -- and their underlying coins
can still be *swapped*, since that needs no zap at all.
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
    """One deposit zap, and which of the four dialects it speaks.

    All four were probed against deployed contracts; none of them can be
    inferred from the others, and a wrong guess is calldata for a function
    that does not exist.
    """

    address: str
    #: Underlying coins it expects: 1 + the base pool's own count. Part of
    #: the signature in the fixed dialect, and merely required in the
    #: dynamic one -- the NG zap reverts on a wrong length either way.
    coins: int
    #: `uint256[]` rather than `uint256[N]`.
    dynamic: bool
    #: Does it take the pool as its first argument? A factory zap serves
    #: every metapool on one base pool and must be told which; the older
    #: crypto metapools each got a zap of their own, which takes none --
    #: its calldata is exactly what the pool itself would take.
    pool_arg: bool = True
    #: Stable zaps carry the `is_deposit` flag on `calc_token_amount` and
    #: index coins with `int128`; the crypto ones have neither.
    stableswap: bool = True
    #: Does it swap, as well as deposit and withdraw? Only the per-pool
    #: crypto zaps do -- all seven carry `exchange_underlying` and
    #: `get_dy_underlying`, checked in their deployed bytecode, and no
    #: factory zap of either kind has either function. It matters because
    #: a crypto metapool has no underlying swap of its own, so without
    #: this the route would not exist for those pools at all.
    swaps: bool = False


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


#: Shared zaps for the crypto factory's metapools, keyed by base pool.
#: `calc_token_amount(address,uint256[N])` -- a pool argument like the
#: stable factory zaps, but no `is_deposit` flag and uint256 indices.
CRYPTO_ZAPS: dict[tuple[int, str], Zap] = {
    (1, "0xdcef968d416a41cdac0ed8702fac8128a64241a2"): Zap(
        "0x5De4EF4879F4fe3bBADF2227D2aC5d0E2D76C895", 3, False, stableswap=False
    ),  # Ethereum, 51 pools
    (1, "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"): Zap(
        "0x97aDC08FA1D849D2C48C5dcC1DaB568B169b0267", 4, False, stableswap=False
    ),  # Ethereum, 24 pools
}

#: Zaps deployed for one pool each, which is how the older crypto
#: metapools were shipped: no pool argument at all, so the calldata is
#: what the pool itself would take -- just addressed elsewhere.
POOL_ZAPS: dict[tuple[int, str], Zap] = {
    (1, "0xadcfcf9894335dc340f6cd182afa45999f45fc44"): Zap(
        "0xc5FA220347375ac4f91f9E4A4AAb362F22801504", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Ethereum Curve XAUT-3Crv
    (1, "0xe84f5b1582ba325fdf9ce6b0c1f087ccfc924e54"): Zap(
        "0xd446A98F88E1d053d1F64986E3Ed083bb1Ab7E5A", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Ethereum Curve EUROC-3Crv
    (1, "0x9838eccc42659fa8aa7daf2ad134b53984c9427b"): Zap(
        "0x5D0F47B32fDd343BfA74cE221808e2abE4A53827", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Ethereum Curve EURT-3Crv
    (100, "0x056c6c5e684cec248635ed86033378cc444459b0"): Zap(
        "0xE3FFF29d4DC930EBb787FeCd49Ee5963DADf60b6", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Gnosis Curve EURe-3Crv
    (137, "0xb446bf7b8d6d4276d0c75ec0e3ee8dd7fe15783a"): Zap(
        "0x225FB4176f0E20CDb66b4a3DF70CA3063281E855", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Polygon Curve EURT-3Crv
    (137, "0x9b3d675fdbe6a0935e8b7d1941bc6f78253549b7"): Zap(
        "0x4DF7eF55E99a56851187822d96B4E17D98A47DeD", 4, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Polygon Curve EURS-3Crv
    (42161, "0xa827a652ead76c6b0b3d19dba05452e06e25c27e"): Zap(
        "0x25e2e8d104BC1A70492e2BE32dA7c1f8367F9d2c", 3, False,
        pool_arg=False, stableswap=False, swaps=True,
    ),  # Arbitrum Curve EURS-2Crv
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
    # Most specific first: a zap deployed for this pool alone.
    zap = POOL_ZAPS.get((pool.chain_id, pool.address.lower()))
    if zap is None:
        if not pool.is_stableswap:
            zap = CRYPTO_ZAPS.get((pool.chain_id, pool.base_pool.lower()))
        elif pool.registry_key in FACTORY_METAPOOL_TYPES:
            zap = ZAPS.get(
                (pool.chain_id, pool.base_pool.lower(), pool.dynamic_arrays)
            )
    if zap is None or len(pool.display_coins) != zap.coins:
        return None
    return zap
