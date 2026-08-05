"""Parsing Curve's API shapes, and the derived numbers the UI sorts on.

The raw fixtures below are trimmed copies of real `getPools`/`getVolumes`
responses, including the awkward parts: numbers as strings, nulls where a
float is expected, and a CRV entry duplicated into `gaugeRewards`.
"""

from __future__ import annotations

from curve.models import Pool, attach_volumes

RAW_3POOL = {
    "address": "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7",
    "name": "Curve.fi DAI/USDC/USDT",
    "symbol": "3Crv",
    "blockchainId": "ethereum",
    "registryId": "main",
    "lpTokenAddress": "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490",
    "usdTotal": 159976570.6,
    "gaugeAddress": "0xbfcf63294ad7105dea65aa58f8ae5be2d9d0952a",
    "gaugeCrvApy": [1.5674e-05, 3.9185e-05],
    "gaugeRewards": [],
    "isMetaPool": False,
    "amplificationCoefficient": "4000",
    "virtualPrice": "1039823717345032546",
    "coins": [
        {
            "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
            "symbol": "DAI",
            "decimals": "18",
            "usdPrice": 1.00001,
            "poolBalance": "25628962988832000000000000",
        },
        {
            "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "symbol": "USDC",
            "decimals": "6",
            "usdPrice": 1.0,
            "poolBalance": "25504448112464",
        },
    ],
}


def test_parses_string_numbers() -> None:
    pool = Pool.from_api(RAW_3POOL)
    assert pool.amplification == 4000
    assert pool.virtual_price == 1039823717345032546
    assert pool.coins[0].decimals == 18
    assert pool.coins[1].decimals == 6
    assert pool.coins[1].pool_balance == 25504448112464


def test_coin_balance_respects_decimals() -> None:
    pool = Pool.from_api(RAW_3POOL)
    assert pool.coins[0].balance == 25628962.988832
    assert round(pool.coins[1].balance, 6) == 25504448.112464


def test_missing_and_null_fields_default_rather_than_raise() -> None:
    pool = Pool.from_api({"address": "0xabc", "coins": [{}]})
    assert pool.tvl == 0.0
    assert pool.crv_apr == (0.0, 0.0)
    assert pool.coins[0].symbol == "?"
    assert pool.coins[0].decimals == 18  # the safe ERC-20 default
    assert pool.n_coins == 1


def test_crv_apr_with_a_single_entry_repeats_it() -> None:
    pool = Pool.from_api({"address": "0x1", "gaugeCrvApy": [2.5]})
    assert pool.crv_apr == (2.5, 2.5)


def test_registry_decides_the_abi_variant() -> None:
    for registry in ("main", "factory", "factory-crvusd", "factory-stable-ng"):
        assert Pool.from_api({"registryId": registry}).is_stableswap
    for registry in ("crypto", "factory-crypto", "factory-twocrypto", "factory-tricrypto"):
        assert not Pool.from_api({"registryId": registry}).is_stableswap


def test_unknown_registry_defaults_to_stableswap() -> None:
    assert Pool.from_api({"registryId": "factory-something-new"}).is_stableswap


def test_crv_is_not_double_counted_as_an_incentive() -> None:
    """Some pools list CRV in `gaugeRewards` as well as `gaugeCrvApy`."""
    pool = Pool.from_api(
        {
            "gaugeCrvApy": [2.93, 7.32],
            "gaugeRewards": [
                {"symbol": "CRV", "apy": 7.32, "tokenAddress": "0xD533"},
                {"symbol": "YB", "apy": 1.5, "tokenAddress": "0xabc"},
            ],
        }
    )
    assert [i.symbol for i in pool.incentives] == ["YB"]
    assert pool.incentives_apr == 7.32 + 1.5


def test_incentives_apr_uses_max_boost() -> None:
    pool = Pool.from_api({"gaugeCrvApy": [1.0, 2.5], "gaugeRewards": []})
    assert pool.incentives_apr == 2.5


def test_total_apr_adds_base() -> None:
    pool = Pool.from_api({"gaugeCrvApy": [1.0, 2.0]})
    pool.base_apr = 0.5
    assert pool.total_apr == 2.5


def test_has_gauge() -> None:
    assert Pool.from_api(RAW_3POOL).has_gauge
    assert not Pool.from_api({"gaugeAddress": None}).has_gauge


def test_display_name_prefers_symbol_then_strips_prefix() -> None:
    assert Pool.from_api(RAW_3POOL).display_name == "3Crv"
    assert Pool.from_api({"name": "Curve.fi DAI/USDC"}).display_name == "DAI/USDC"
    assert Pool.from_api({"address": "0x1234567890abcdef"}).display_name == "0x12345678"


def test_chain_falls_back_to_the_argument() -> None:
    """The single-registry endpoint omits blockchainId; the big one has it."""
    assert Pool.from_api({"address": "0x1"}, "arbitrum").chain == "arbitrum"
    assert Pool.from_api(RAW_3POOL, "arbitrum").chain == "ethereum"


# -- the volume join -------------------------------------------------------


def test_attach_volumes_matches_case_insensitively() -> None:
    pool = Pool.from_api(RAW_3POOL)
    attach_volumes(
        [pool],
        [
            {
                "address": pool.address.lower(),
                "volumeUSD": 19006971.12,
                "latestWeeklyApyPcent": 1.27,
            }
        ],
    )
    assert pool.volume_24h == 19006971.12
    assert pool.base_apr == 1.27


def test_attach_volumes_leaves_unmatched_pools_alone() -> None:
    pool = Pool.from_api(RAW_3POOL)
    attach_volumes([pool], [{"address": "0xdeadbeef", "volumeUSD": 5.0}])
    assert pool.volume_24h == 0.0


def test_attach_volumes_ignores_rows_without_an_address() -> None:
    pool = Pool.from_api(RAW_3POOL)
    attach_volumes([pool], [{"volumeUSD": 1.0}, {"address": None}])
    assert pool.volume_24h == 0.0
