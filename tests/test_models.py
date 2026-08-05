"""Parsing the Prices API v2 shapes, and the numbers the UI sorts on.

The fixtures below are trimmed copies of real `/v2/pools/` and
`/v2/pools/{chain_id}/{address}` responses, including the awkward parts:
`gauges` arriving as objects on one endpoint and strings on the other, and
a list payload that simply lacks the LP token and the reserves.
"""

from __future__ import annotations

from curve.models import Pool

# One entry of `/v2/pools/?chain_id=1`.
LIST_3POOL = {
    "chain_id": 1,
    "name": "Curve.fi DAI/USDC/USDT",
    "address": "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
    "creation_date": 1599422178,
    "pool_type": "main",
    "is_metapool": False,
    "base_pool": None,
    "tvl_usd": 160012141.759445,
    "trading_volume_24h": 17281820.13,
    "coins": [
        {
            "pool_index": 0,
            "symbol": "DAI",
            "name": "Dai Stablecoin",
            "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "decimals": 18,
        },
        {
            "pool_index": 1,
            "symbol": "USDC",
            "name": "USD Coin",
            "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "decimals": 6,
        },
    ],
    "base_daily_apr": 0.0001,
    "base_weekly_apr": 0.0002,
    "crv_apr": 1.56,
    "crv_apr_boosted": 3.90,
    "extra_rewards_apr": [],
    "merkle_apr": 0.0,
    # The list endpoint's shape: objects with a kill flag.
    "gauges": [{"address": "0xbfcf63294ad7105dea65aa58f8ae5be2d9d0952a", "is_killed": False}],
}

# The same pool from `/v2/pools/1/0xbEbc…`.
DETAIL_3POOL = {
    "lp_token_address": "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490",
    "registry_type": "main",
    "n_coins": 2,
    # The detail endpoint's shape: bare strings.
    "gauges": ["0xbFcF63294aD7105dEa65aA58F8AE5BE2D9d0952A"],
    "coins": [
        {
            "pool_index": 0,
            "symbol": "DAI",
            "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "decimals": 18,
            "usd_price": 1.00005,
        },
        {
            "pool_index": 1,
            "symbol": "USDC",
            "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "decimals": 6,
            "usd_price": 1.0,
        },
    ],
    "balances": [25630533.14, 25503596.21],
    "balances_usd": [25631776.37, 25503596.21],
    "metadata": {"a": 4000.0, "virtual_price": 1.0398237173447054e18, "fee": 1500000.0},
}


def test_parses_a_list_entry() -> None:
    pool = Pool.from_v2(LIST_3POOL, "ethereum")
    assert pool.chain == "ethereum"
    assert pool.chain_id == 1
    assert pool.registry == "main"
    assert pool.tvl == 160012141.759445
    assert pool.volume_24h == 17281820.13
    assert pool.n_coins == 2
    assert pool.coin_symbols == ["DAI", "USDC"]


def test_base_apr_comes_straight_off_the_pool() -> None:
    """v2 needs no join: v1 had to merge getVolumes onto getPools by address."""
    pool = Pool.from_v2(LIST_3POOL)
    assert pool.base_apr == 0.0002  # weekly, matching Curve's own column


def test_crv_range_is_the_boost_pair() -> None:
    assert Pool.from_v2(LIST_3POOL).crv_apr == (1.56, 3.90)


def test_gauge_parsed_from_the_list_shape() -> None:
    assert Pool.from_v2(LIST_3POOL).gauge.lower().startswith("0xbfcf")


def test_gauge_parsed_from_the_detail_shape() -> None:
    """The two endpoints disagree: objects on the list, strings on detail."""
    pool = Pool.from_v2(LIST_3POOL).merge_detail(DETAIL_3POOL)
    assert pool.gauge == "0xbFcF63294aD7105dEa65aA58F8AE5BE2D9d0952A"


def test_killed_gauges_are_ignored() -> None:
    """A killed gauge takes deposits and pays nothing; never offer it."""
    pool = Pool.from_v2(
        {"gauges": [{"address": "0xdead", "is_killed": True}]}
    )
    assert not pool.has_gauge
    live = Pool.from_v2(
        {
            "gauges": [
                {"address": "0xdead", "is_killed": True},
                {"address": "0xlive", "is_killed": False},
            ]
        }
    )
    assert live.gauge == "0xlive"


def test_missing_and_null_fields_default_rather_than_raise() -> None:
    pool = Pool.from_v2({"address": "0xabc", "coins": [{}]})
    assert pool.tvl == 0.0
    assert pool.crv_apr == (0.0, 0.0)
    assert pool.coins[0].symbol == "?"
    assert pool.coins[0].decimals == 18  # the safe ERC-20 default
    assert not pool.has_gauge
    assert not pool.detailed


def test_pool_type_decides_the_abi_variant() -> None:
    for kind in ("main", "factory", "crvusd", "stableswapng"):
        assert Pool.from_v2({"pool_type": kind}).is_stableswap
    for kind in ("crypto", "factory_crypto", "factory_tricrypto", "twocryptong"):
        assert not Pool.from_v2({"pool_type": kind}).is_stableswap


def test_v1_registry_spellings_still_dispatch() -> None:
    """Kept as aliases so a Pool from either API generation is safe."""
    assert Pool.from_v2({"pool_type": "factory-stable-ng"}).is_stableswap
    assert not Pool.from_v2({"pool_type": "factory-twocrypto"}).is_stableswap


def test_unknown_pool_type_defaults_to_stableswap() -> None:
    assert Pool.from_v2({"pool_type": "some-new-factory"}).is_stableswap


def test_crv_is_not_double_counted_as_an_incentive() -> None:
    pool = Pool.from_v2(
        {
            "crv_apr": 2.93,
            "crv_apr_boosted": 7.32,
            "extra_rewards_apr": [
                {"symbol": "CRV", "apr": 7.32},
                {"symbol": "YB", "apr": 1.5},
            ],
        }
    )
    assert [i.symbol for i in pool.incentives] == ["YB"]
    assert pool.incentives_apr == 7.32 + 1.5


def test_incentives_apr_includes_merkle() -> None:
    """v1 had no merkle field at all, so this column is new."""
    pool = Pool.from_v2(
        {"crv_apr": 1.0, "crv_apr_boosted": 2.0, "merkle_apr": 322.4}
    )
    assert pool.incentives_apr == 2.0 + 322.4


def test_total_apr_adds_base() -> None:
    pool = Pool.from_v2({"crv_apr": 1.0, "crv_apr_boosted": 2.0, "base_weekly_apr": 0.5})
    assert pool.total_apr == 2.5


def test_display_name_strips_curve_prefixes() -> None:
    assert Pool.from_v2(LIST_3POOL).display_name == "DAI/USDC/USDT"
    assert (
        Pool.from_v2({"name": "Curve.fi Factory Plain Pool: stETH"}).display_name == "stETH"
    )
    assert Pool.from_v2({"address": "0x1234567890abcdef"}).display_name == "0x12345678"


# -- merge_detail ----------------------------------------------------------


def test_merge_detail_supplies_what_the_list_lacks() -> None:
    pool = Pool.from_v2(LIST_3POOL, "ethereum")
    # None of this is in the list payload -- and without the LP token there
    # is nothing to withdraw or stake.
    assert not pool.lp_token
    assert pool.coins[0].balance == 0.0

    pool.merge_detail(DETAIL_3POOL)
    assert pool.detailed
    assert pool.lp_token == "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490"
    assert pool.coins[0].balance == 25630533.14
    assert pool.coins[0].balance_usd == 25631776.37
    assert pool.coins[0].usd_price == 1.00005
    assert pool.amplification == 4000.0
    assert pool.virtual_price == 1.0398237173447054e18


def test_merge_detail_survives_a_sparse_payload() -> None:
    pool = Pool.from_v2(LIST_3POOL).merge_detail({})
    assert pool.detailed
    # Falls back to the pool's own address so a swap still has a target.
    assert pool.lp_token == LIST_3POOL["address"]


def test_merge_detail_tolerates_short_balance_arrays() -> None:
    pool = Pool.from_v2(LIST_3POOL).merge_detail({**DETAIL_3POOL, "balances": [1.0]})
    assert pool.coins[0].balance == 1.0
    assert pool.coins[1].balance == 0.0
