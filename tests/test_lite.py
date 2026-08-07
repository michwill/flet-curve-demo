"""Curve Lite: a second API, and rather less of it.

These chains run the Curve contracts without the indexing the big ones
have, and are served by `api2.curve.finance` instead of the Prices API.
Three differences drive everything here, and all three are about *absence*:

  * no volume, no base APR, no CRV boost range, no price history. Not
    zero -- unmeasured. A list that printed 0.00% would be reporting a
    measurement nobody took, so the columns come out instead;
  * the whole chain arrives in one response rather than a page at a time,
    so ordering, searching and paging happen locally, and must come out
    the same shape `PoolFeed` already expects;
  * a pool arrives complete, so there is no detail call to make.

Every payload below is trimmed from a real response: Etherlink and Sonic
through `get_pools`, and the `get_platforms` metadata as it stands.
"""

from __future__ import annotations

import pytest

from curve.lite import (
    LITE_MIN_TVL,
    LiteChain,
    matches,
    parse_hidden,
    parse_platforms,
    parse_pools,
    select,
)
from curve.models import Pool
from curve.sort import get_sort

PLATFORMS = {
    "success": True,
    "data": {
        "platforms": {"etherlink": ["factory_stable_ng"], "monad": ["factory_twocrypto"]},
        "platforms_metadata": {
            "etherlink": {
                "is_mainnet": True,
                "name": "Etherlink",
                "chain_id": 42793,
                "tvl": 9138337.3,
            },
            "x-layer": {
                "is_mainnet": True,
                "name": "X layer",
                "chain_id": 196,
                "tvl": 3009966.0,
            },
            "bsc_testnet": {
                "is_mainnet": False,
                "name": "BSC Testnet",
                "chain_id": 97,
                "tvl": 0,
            },
            "nameless": {"is_mainnet": True, "name": "Nowhere", "tvl": 1.0},
        },
    },
}


def lite_pool(
    address: str = "0x" + "ab" * 20,
    *,
    name: str = "mBASIS/USDC",
    tvl: float = 2_289_892.0,
    registry: str = "factory_stable_ng",
    gauge: str = "0x" + "cd" * 20,
    killed: bool = False,
    broken: bool = False,
    chain_id: int = 42793,
) -> dict:
    """One `pool_data` entry, shaped as the API sends it.

    Note the string decimals and the raw `pool_balance`: v2 sends reserves
    already scaled, this does not.
    """
    return {
        "id": "factory_stable_ng-0",
        "chain_id": chain_id,
        "address": address,
        "registry_id": registry,
        "name": name,
        "symbol": "mBASISUSDC",
        "total_supply": "1000000000000000000",
        "tvl": tvl,
        "coins": [
            {
                "address": "0x" + "11" * 20,
                "usd_price": 1.203,
                "decimals": "18",
                "is_base_pool_lp_token": False,
                "symbol": "mBASIS",
                "pool_balance": "933686006400000000000000",
            },
            {
                "address": "0x" + "22" * 20,
                "usd_price": 1.0,
                "decimals": "6",
                "is_base_pool_lp_token": False,
                "symbol": "USDC",
                "pool_balance": "1166661000000",
            },
        ],
        "virtual_price": 1041077047347782051,
        "amplification_coefficient": "500",
        "lp_token_address": address,
        "is_meta_pool": False,
        "is_broken": broken,
        "gauge_address": gauge,
        "gauge_is_killed": killed,
        "gauge_crv_apy": None,
        "gauge_extra_rewards": [],
    }


# -- platforms -------------------------------------------------------------


def test_mainnets_are_offered() -> None:
    chains = parse_platforms(PLATFORMS)
    assert chains["etherlink"] == LiteChain("etherlink", 42793, "Etherlink", 9138337.3)
    assert chains["x-layer"].label == "X layer"


def test_testnets_are_not() -> None:
    """They are real entries with real pools and no use to anyone opening
    a pool list."""
    assert "bsc_testnet" not in parse_platforms(PLATFORMS)


def test_an_entry_with_no_chain_id_is_skipped() -> None:
    assert "nameless" not in parse_platforms(PLATFORMS)


def test_nothing_at_all_is_not_an_error() -> None:
    """A Lite API that is down should cost its chains, not the app."""
    assert parse_platforms(None) == {}
    assert parse_platforms({"data": {}}) == {}


# -- pools -----------------------------------------------------------------


def payload(*pools: dict) -> dict:
    return {"success": True, "data": {"pool_data": list(pools), "tvl": 1.0}}


def test_a_pool_carries_what_the_chain_knows() -> None:
    (pool,) = parse_pools(payload(lite_pool()), "etherlink")
    assert pool.lite is True
    assert pool.chain == "etherlink"
    assert pool.chain_id == 42793
    assert pool.tvl == pytest.approx(2_289_892.0)
    assert pool.coin_symbols == ["mBASIS", "USDC"]
    assert pool.lp_token == pool.address
    assert pool.amplification == 500
    # 1e18 fixed point there, a plain float here.
    assert pool.virtual_price == pytest.approx(1.041077047347782)
    # Complete on arrival: there is no detail endpoint to fill it in.
    assert pool.detailed is True


def test_what_is_unmeasured_stays_at_zero_but_is_marked() -> None:
    """The `lite` flag is what stops a nought being read as a measurement:
    the view drops those columns rather than printing them."""
    (pool,) = parse_pools(payload(lite_pool()), "etherlink")
    assert (pool.volume_24h, pool.base_apr, pool.crv_apr) == (0.0, 0.0, (0.0, 0.0))
    assert pool.lite is True


def test_reserves_are_scaled_by_their_own_decimals() -> None:
    """Raw integers and string decimals, where v2 sends human numbers."""
    (pool,) = parse_pools(payload(lite_pool()), "etherlink")
    mbasis, usdc = pool.coins
    assert mbasis.balance == pytest.approx(933_686.0064)
    assert usdc.balance == pytest.approx(1_166_661.0)  # six decimals, not eighteen
    assert usdc.balance_usd == pytest.approx(1_166_661.0)
    assert mbasis.balance_usd == pytest.approx(933_686.0064 * 1.203)


def test_the_registry_id_still_picks_the_right_abi() -> None:
    """Underscores where v2 writes nothing and v1 writes hyphens. Getting
    this wrong sends a fixed-array deposit to a DynArray pool."""
    (ng,) = parse_pools(payload(lite_pool(registry="factory_stable_ng")), "etherlink")
    assert ng.is_stableswap is True
    assert ng.dynamic_arrays is True

    (two,) = parse_pools(payload(lite_pool(registry="factory_twocrypto")), "monad")
    assert two.is_stableswap is False
    assert two.dynamic_arrays is False

    (tri,) = parse_pools(payload(lite_pool(registry="factory_tricrypto")), "monad")
    assert tri.is_stableswap is False


def test_a_killed_gauge_is_no_gauge() -> None:
    (pool,) = parse_pools(payload(lite_pool(killed=True)), "etherlink")
    assert pool.has_gauge is False


def test_broken_pools_are_dropped() -> None:
    """The API marks these itself, and a pool that cannot be read is worse
    than one that is absent."""
    assert parse_pools(payload(lite_pool(broken=True)), "etherlink") == []


def test_hidden_pools_are_dropped() -> None:
    hidden = parse_hidden(
        {"data": [{"chain_id": 42793, "address": "0x" + "AB" * 20}]}
    )
    assert parse_pools(payload(lite_pool()), "etherlink", hidden) == []
    # Same address, another chain: not the same pool.
    assert len(parse_pools(payload(lite_pool(chain_id=196)), "x-layer", hidden)) == 1


def test_a_reward_without_a_price_is_not_an_apr() -> None:
    """`apy` is null wherever the token has no price, which on these chains
    is most of the time. The emission rate is not a substitute."""
    raw = lite_pool()
    raw["gauge_extra_rewards"] = [
        {
            "token_address": "0x" + "33" * 20,
            "symbol": "WXPL",
            "apy": None,
            "apy_data": {"rate": 4.96e-06, "is_reward_still_active": False},
        },
        {"token_address": "0x" + "44" * 20, "symbol": "UNO", "apy": 12.5},
    ]
    (pool,) = parse_pools(payload(raw), "plasma")
    assert [(i.symbol, i.apr) for i in pool.incentives] == [("WXPL", 0.0), ("UNO", 12.5)]


def test_a_metapool_keeps_the_coins_the_contract_has() -> None:
    """v2 decomposes a metapool and this API does not, so there is nothing
    to drop -- and with no base pool address, no zap route either."""
    raw = lite_pool(name="crvUSD/2CRV")
    raw["is_meta_pool"] = True
    (pool,) = parse_pools(payload(raw), "x-layer")
    assert pool.is_meta is True
    assert pool.base_pool == ""
    assert pool.display_coins == pool.pool_coins
    from curve.zaps import zap_for

    assert zap_for(pool) is None


# -- the local page ---------------------------------------------------------
#
# `select` stands in for what v2 does with query parameters, and has to
# return the same (page, total) contract so `PoolFeed` cannot tell.


def spread(count: int) -> list[Pool]:
    return [
        parse_pools(
            payload(
                lite_pool(
                    address="0x" + f"{index:040x}",
                    name=f"Pool {index}",
                    tvl=float(index),
                )
            ),
            "etherlink",
        )[0]
        for index in range(count)
    ]


def test_a_page_and_a_total() -> None:
    page, total = select(spread(7), sort_local=get_sort("tvl").local, page=1, page_size=3)
    assert total == 7
    assert [p.name for p in page] == ["Pool 6", "Pool 5", "Pool 4"]


def test_later_pages_continue_the_order() -> None:
    pools = spread(7)
    second, _ = select(pools, sort_local=get_sort("tvl").local, page=2, page_size=3)
    assert [p.name for p in second] == ["Pool 3", "Pool 2", "Pool 1"]


def test_a_page_past_the_end_is_empty_not_an_error() -> None:
    page, total = select(spread(3), sort_local=get_sort("tvl").local, page=9, page_size=3)
    assert page == [] and total == 3


def test_ascending_is_honoured() -> None:
    page, _ = select(
        spread(4), sort_local=get_sort("tvl").local, direction="asc", page_size=2
    )
    assert [p.name for p in page] == ["Pool 0", "Pool 1"]


def test_search_matches_name_symbol_or_address() -> None:
    pools = parse_pools(payload(lite_pool()), "etherlink")
    assert matches(pools[0], "mbasis")  # coin symbol, case-insensitively
    assert matches(pools[0], "BASIS/USD")  # pool name
    assert matches(pools[0], "0xabab")  # address prefix
    assert not matches(pools[0], "steth")
    assert matches(pools[0], "  ")  # an empty query matches everything


def test_search_narrows_the_total_too() -> None:
    """The count is what the list prints and what tells the feed when to
    stop paging, so it has to be the *matching* count."""
    # Every pool in `spread` carries the same two coins, so the query has
    # to be the one thing that differs -- the name.
    pools = spread(5) + parse_pools(payload(lite_pool()), "etherlink")
    page, total = select(pools, sort_local=get_sort("tvl").local, search="mBASIS/USDC")
    assert total == 1 and len(page) == 1


def test_the_floor_is_zero_by_default() -> None:
    """Whole Lite deployments are smaller than the $10k floor the big
    chains use -- Sonic's pools come to about $200k between them -- so the
    same cut would empty the list."""
    assert LITE_MIN_TVL == 0
    _page, total = select(spread(5), sort_local=get_sort("tvl").local)
    assert total == 5  # including "Pool 0", worth nothing at all


def test_a_floor_still_applies_when_asked_for() -> None:
    _page, total = select(spread(5), sort_local=get_sort("tvl").local, min_tvl=3)
    assert total == 2


# -- what a slow Lite API is allowed to cost -------------------------------


async def test_the_platforms_call_gives_up_quickly() -> None:
    """It is on the critical path for *every* chain: `chains()` folds the
    Lite deployments into the picker, so the first page of Ethereum pools
    waits on it. At the 30-second default a slow api2 is a pool list that
    looks like it is loading forever."""
    from curve import api as api_module
    from curve.lite import LITE_TIMEOUT

    asked: list[tuple[str, float]] = []

    async def fake_get_json(url, timeout=30.0):
        asked.append((url, timeout))
        return {"data": {"platforms_metadata": {}}}

    api = api_module.CurveApi()
    original = api_module.get_json
    api_module.get_json = fake_get_json
    try:
        await api.lite_chains()
    finally:
        api_module.get_json = original

    from curve.http import DEFAULT_TIMEOUT

    assert asked and asked[0][1] == LITE_TIMEOUT
    assert LITE_TIMEOUT < DEFAULT_TIMEOUT


async def test_a_lite_api_that_never_answers_costs_only_the_lite_chains() -> None:
    """The degradation `lite_chains` promises: the picker loses them, the
    main chains carry on."""
    from curve import api as api_module
    from curve.http import ApiError

    async def timing_out(url, timeout=30.0):
        raise ApiError("timed out")

    api = api_module.CurveApi()
    original = api_module.get_json
    api_module.get_json = timing_out
    try:
        assert await api.lite_chains() == {}
    finally:
        api_module.get_json = original
