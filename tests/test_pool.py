"""`PoolContract` driven against a fake EIP-1193 provider.

No network and no wallet: the provider below records what it was asked and
answers from a script, which is enough to pin down both the calldata that
goes out and the handling of what comes back.

The empty-data cases are the ones that matter most. Calling a function a
pool does not implement returns `0x` rather than reverting, and `0x` decodes
to zero -- so without the guard in `PoolContract._read` a mis-typed pool
would quote every swap at zero output instead of failing visibly.
"""

from __future__ import annotations

import pytest

from curve import abi
from curve.models import Pool
from curve.pool import PoolCallFailed, PoolContract
from wallet.base import RpcError, WalletProvider

ACCOUNT = "0x1111111111111111111111111111111111111111"
POOL_ADDRESS = "0x390f3595bCa2df7D23783DFd126427CCeb997BF4"
GAUGE = "0x4E6bB6B7447B7B2Aa268C16AB87F4Bb48BF57939"
LP_TOKEN = "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490"


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


class FakeProvider(WalletProvider):
    """Answers `eth_call` from a script and records every request."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []
        self.sent: list[dict] = []
        self.default = word(0)
        self.raise_on_call: Exception | None = None

    async def request(self, method: str, params=None):
        params = params or []
        if method == "eth_call":
            to, data = params[0]["to"], params[0]["data"]
            self.calls.append((to, data))
            if self.raise_on_call is not None:
                raise self.raise_on_call
            # Match on selector so tests need not spell out full calldata.
            return self.answers.get(data[:10], self.default)
        if method == "eth_sendTransaction":
            self.sent.append(params[0])
            return "0x" + "ab" * 32
        raise AssertionError(f"unexpected method {method}")


def make_pool(registry: str = "crvusd", *, gauge: str = GAUGE) -> Pool:
    return Pool.from_v2(
        {
            "address": POOL_ADDRESS,
            "pool_type": registry,
            "lp_token_address": LP_TOKEN,
            "gauges": [{"address": gauge, "is_killed": False}] if gauge else [],
            "coins": [
                {"symbol": "USDT", "address": "0x" + "aa" * 20, "decimals": 6},
                {"symbol": "crvUSD", "address": "0x" + "bb" * 20, "decimals": 18},
            ],
        }
    )


def contract(provider: FakeProvider, pool: Pool | None = None) -> PoolContract:
    return PoolContract(provider, pool or make_pool(), ACCOUNT)


# -- the empty-data guard --------------------------------------------------


async def test_empty_return_data_is_an_error_not_a_zero_quote() -> None:
    provider = FakeProvider()
    provider.default = "0x"
    with pytest.raises(PoolCallFailed, match="did not answer"):
        await contract(provider).get_dy(0, 1, 10**6)


async def test_bare_0x0_is_also_rejected() -> None:
    provider = FakeProvider()
    provider.default = "0x0"
    with pytest.raises(PoolCallFailed):
        await contract(provider).calc_withdraw_one_coin(10**18, 0)


async def test_a_genuine_zero_quote_is_still_returned() -> None:
    """A full 32-byte zero is a real answer, unlike empty data."""
    provider = FakeProvider({"0x" + abi.selector("get_dy(int128,int128,uint256)"): word(0)})
    assert await contract(provider).get_dy(0, 1, 10**6) == 0


async def test_rpc_errors_become_pool_errors() -> None:
    provider = FakeProvider()
    provider.raise_on_call = RpcError(-32000, "execution reverted")
    with pytest.raises(PoolCallFailed, match="execution reverted"):
        await contract(provider).get_dy(0, 1, 10**6)


# -- reads -----------------------------------------------------------------


async def test_get_dy_uses_the_stableswap_selector_for_a_stable_pool() -> None:
    provider = FakeProvider()
    provider.default = word(999)
    await contract(provider, make_pool("crvusd")).get_dy(0, 1, 1000)
    to, data = provider.calls[-1]
    assert to == POOL_ADDRESS
    assert data.startswith("0x" + abi.selector("get_dy(int128,int128,uint256)"))


async def test_get_dy_uses_the_crypto_selector_for_a_crypto_pool() -> None:
    provider = FakeProvider()
    provider.default = word(999)
    await contract(provider, make_pool("twocryptong")).get_dy(0, 1, 1000)
    _, data = provider.calls[-1]
    assert data.startswith("0x" + abi.selector("get_dy(uint256,uint256,uint256)"))


async def test_get_dy_short_circuits_on_zero_input() -> None:
    provider = FakeProvider()
    assert await contract(provider).get_dy(0, 1, 0) == 0
    assert provider.calls == []  # no pointless round trip


async def test_calc_token_amount_falls_back_to_the_older_signature() -> None:
    """Old CryptoSwap pools declare `calc_token_amount(uint256[N])`."""
    with_flag = "0x" + abi.selector("calc_token_amount(uint256[2],bool)")
    without = "0x" + abi.selector("calc_token_amount(uint256[2])")
    provider = FakeProvider({with_flag: "0x", without: word(4242)})
    assert await contract(provider).calc_token_amount([1, 0]) == 4242
    assert [d[:10] for _, d in provider.calls] == [with_flag, without]


async def test_calc_token_amount_skips_the_call_when_nothing_is_entered() -> None:
    provider = FakeProvider()
    assert await contract(provider).calc_token_amount([0, 0]) == 0
    assert provider.calls == []


async def test_balance_of_allows_a_real_zero() -> None:
    """Unlike a quote, an empty balance is a perfectly good answer."""
    provider = FakeProvider()
    provider.default = word(0)
    assert await contract(provider).balance_of("0x" + "cc" * 20) == 0


async def test_staked_balance_is_zero_without_a_gauge() -> None:
    provider = FakeProvider()
    pool = make_pool(gauge="")
    assert await contract(provider, pool).staked_balance() == 0
    assert provider.calls == []


# -- writes ----------------------------------------------------------------


async def test_exchange_sends_the_expected_transaction() -> None:
    provider = FakeProvider()
    await contract(provider).exchange(0, 1, 1000, 995)
    tx = provider.sent[-1]
    assert tx["from"] == ACCOUNT
    assert tx["to"] == POOL_ADDRESS
    assert tx["value"] == "0x0"
    assert tx["data"] == abi.encode_exchange(0, 1, 1000, 995, stableswap=True)
    # Gas and nonce are the wallet's job.
    assert "gas" not in tx and "nonce" not in tx


async def test_add_liquidity_encodes_every_coin() -> None:
    provider = FakeProvider()
    await contract(provider).add_liquidity([1000, 2000], 42)
    assert provider.sent[-1]["data"] == abi.encode_add_liquidity([1000, 2000], 42)


async def test_stake_targets_the_gauge_not_the_pool() -> None:
    provider = FakeProvider()
    await contract(provider).stake(500)
    tx = provider.sent[-1]
    assert tx["to"] == GAUGE
    assert tx["data"] == abi.encode_gauge_deposit(500)


async def test_unstake_targets_the_gauge() -> None:
    provider = FakeProvider()
    await contract(provider).unstake(500)
    assert provider.sent[-1]["data"] == abi.encode_gauge_withdraw(500)


async def test_staking_without_a_gauge_is_refused_before_sending() -> None:
    provider = FakeProvider()
    pool = make_pool(gauge="")
    with pytest.raises(PoolCallFailed, match="no gauge"):
        await contract(provider, pool).stake(1)
    assert provider.sent == []


async def test_approve_is_for_an_exact_amount_not_unlimited() -> None:
    provider = FakeProvider()
    token = "0x" + "cc" * 20
    await contract(provider).approve(token, POOL_ADDRESS, 12345)
    tx = provider.sent[-1]
    assert tx["to"] == token
    assert tx["data"] == abi.encode_approve(POOL_ADDRESS, 12345)
    assert abi.decode_uint("0x" + tx["data"][74:]) == 12345
    assert abi.decode_uint("0x" + tx["data"][74:]) != abi.MAX_UINT256
