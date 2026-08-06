"""Metapools: depositing the underlying coins through a zap.

A metapool contract holds two coins -- its own, and the base pool's LP
token -- and almost nobody holds that LP token. The zap route lets the
underlying coins be deposited directly, and everything in this file is
about the ways that route differs from the plain one:

  * a different contract to send to, carrying the pool as an argument;
  * a different set of coins, and so a different set of approvals, whose
    spender is the zap rather than the pool;
  * two ABI dialects, `uint256[]` and `uint256[N]`, as in the pools;
  * two fees, because the deposit passes through both pools.

The selectors below were read off the deployed Ethereum zaps, and the
whole flow was run on a mainnet fork through titanoboa: the calldata these
encoders produce was accepted by both zaps, minted above the floor, and
came back out again. See docs/slippage.md for the fee measurements.
"""

from __future__ import annotations

import pytest

from curve import abi
from curve.models import Coin, Pool
from curve.pool import PoolCallFailed, PoolContract
from curve.zaps import ZAPS, zap_for

from test_actions import ACCOUNT, FakeProvider, StubPage, word, words_of

NG_ZAP = "0xE07a16358aA878CBDa2D49A88E5106871E0db307"
LEGACY_ZAP = "0xA79828DF1850E8a3A3064576f380D90aECDD3359"
THREEPOOL = "0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7"
RESERVES = "0x4f493B7dE8aAC7d55F71853688b1F7C8F0243C85"  # Strategic USD Reserves
META = "0xC09e82f81Cb811DB0922dD48206fc2e212322caf"  # World Liberty USD1


def metapool(
    registry: str = "stableswapng",
    base_pool: str = RESERVES,
    coins: int = 4,
    chain_id: int = 1,
) -> Pool:
    """A metapool as v2 reports one: coins decomposed, `n_coins` = 2.

    Four coins means `[USD1, crv2pool, USDC, USDT]` -- the base LP in
    second place, which `display_coins` drops.
    """
    pool = Pool(
        address=META,
        name="World Liberty USD1 Pool",
        chain="ethereum",
        chain_id=chain_id,
        registry=registry,
        base_pool=base_pool,
        lp_token=META,
        coins=[
            Coin("0x" + f"{i + 1:02x}" * 20, f"C{i}", 18 if i % 2 else 6, index=i)
            for i in range(coins)
        ],
    )
    pool.onchain_coins = 2
    return pool


# -- the registry ----------------------------------------------------------


def test_an_ng_metapool_gets_the_ng_zap() -> None:
    zap = zap_for(metapool())
    assert zap is not None
    assert zap.address == NG_ZAP
    assert zap.dynamic is True


def test_an_old_factory_metapool_gets_the_fixed_array_zap() -> None:
    """3pool has two zaps -- an NG one and the old factory's -- and which
    applies follows the *pool's* implementation, not the base pool's."""
    zap = zap_for(metapool(registry="factory", base_pool=THREEPOOL, coins=5))
    assert zap is not None
    assert zap.address == LEGACY_ZAP
    assert zap.dynamic is False


def test_the_oldest_metapools_are_not_offered_a_zap() -> None:
    """`main`-registry metapools predate the factory: each has its own
    deposit contract taking no pool argument, and its LP token is a
    separate contract the factory zap would fail to hand back."""
    assert zap_for(metapool(registry="main", base_pool=THREEPOOL, coins=5)) is None


def test_a_plain_pool_has_no_zap() -> None:
    pool = metapool()
    pool.base_pool = ""
    assert zap_for(pool) is None


def test_an_unknown_base_pool_has_no_zap() -> None:
    assert zap_for(metapool(base_pool="0x" + "99" * 20)) is None


def test_the_same_base_pool_on_another_chain_is_not_assumed() -> None:
    """Curve deploys one zap per factory per chain; addresses do not carry
    across, and an approval to the wrong one would be an approval to
    whatever happens to live there."""
    assert zap_for(metapool(chain_id=8453)) is None


def test_a_coin_list_that_does_not_match_the_zap_is_refused() -> None:
    """The fixed dialect encodes N in the signature, so a mismatch is a
    call to a function that does not exist. Better no zap than that."""
    assert zap_for(metapool(coins=5)) is None  # 4 underlying, zap expects 3


def test_every_registered_zap_has_a_plausible_shape() -> None:
    for (chain_id, base, dynamic), zap in ZAPS.items():
        assert chain_id > 0
        assert base == base.lower() and len(base) == 42
        assert len(zap.address) == 42 and zap.address.startswith("0x")
        assert 3 <= zap.coins <= 5
        assert zap.dynamic is dynamic  # the key and the entry must agree


# -- calldata --------------------------------------------------------------
#
# Selectors read off the deployed zaps; the fixed-array ones are the 3pool
# zap's, whose N is 4.


@pytest.mark.parametrize(
    "signature,expected",
    {
        "calc_token_amount(address,uint256[],bool)": "f558454d",
        "add_liquidity(address,uint256[],uint256)": "fd9de631",
        "remove_liquidity(address,uint256,uint256[])": "8dae1a80",
        "calc_token_amount(address,uint256[4],bool)": "861cdef0",
        "add_liquidity(address,uint256[4],uint256)": "384e03db",
        "remove_liquidity(address,uint256,uint256[4])": "ad5cc918",
        # Identical in both dialects: every zapped metapool is StableSwap,
        # so the coin index is `int128` either way.
        "calc_withdraw_one_coin(address,uint256,int128)": "41b028f3",
        "remove_liquidity_one_coin(address,uint256,int128,uint256)": "29ed2862",
    }.items(),
)
def test_zap_selector_matches_the_deployed_contract(signature, expected) -> None:
    assert abi.selector(signature) == expected


def test_the_dynamic_deposit_carries_an_offset_and_a_length() -> None:
    data = abi.encode_zap_add_liquidity(META, [0, 5, 0], 7, dynamic=True)
    assert data[:10] == "0x" + abi.selector("add_liquidity(address,uint256[],uint256)")
    pool, offset, min_mint, length, *amounts = words_of(data)
    assert pool == int(META, 16)
    assert offset == 3 * 32  # three head words: pool, offset, min_mint
    assert min_mint == 7
    assert length == 3
    assert amounts == [0, 5, 0]


def test_the_fixed_deposit_is_inline_and_its_length_is_in_the_signature() -> None:
    data = abi.encode_zap_add_liquidity(META, [0, 5, 0, 0], 7, dynamic=False)
    assert data[:10] == "0x" + abi.selector("add_liquidity(address,uint256[4],uint256)")
    assert words_of(data) == [int(META, 16), 0, 5, 0, 0, 7]


def test_the_two_dialects_are_different_functions() -> None:
    """Not two encodings of one: sending the wrong one reverts."""
    amounts = [1, 2, 3, 4]
    assert (
        abi.encode_zap_calc_token_amount(META, amounts, dynamic=True)[:10]
        != abi.encode_zap_calc_token_amount(META, amounts, dynamic=False)[:10]
    )


def test_the_withdrawal_estimate_names_the_pool_first() -> None:
    data = abi.encode_zap_calc_withdraw_one_coin(META, 10**18, 2)
    assert words_of(data) == [int(META, 16), 10**18, 2]


# -- the contract layer ----------------------------------------------------


def contract_for(pool: Pool | None = None) -> tuple[PoolContract, FakeProvider]:
    provider = FakeProvider({"0xf558454d": word(42 * 10**18)})
    return PoolContract(provider, pool or metapool(), ACCOUNT), provider


async def test_a_zap_quote_goes_to_the_zap_not_the_pool() -> None:
    contract, provider = contract_for()
    sent: list[dict] = []

    async def record(method, params=None):
        sent.append(params[0])
        return word(42 * 10**18)

    provider.request = record  # type: ignore[method-assign]
    assert await contract.zap_calc_token_amount([1, 0, 0]) == 42 * 10**18
    assert sent[0]["to"] == NG_ZAP
    assert sent[0]["data"][:10] == "0x" + abi.selector(
        "calc_token_amount(address,uint256[],bool)"
    )


async def test_a_zap_deposit_is_sent_to_the_zap() -> None:
    contract, provider = contract_for()
    await contract.zap_add_liquidity([1, 0, 0], 5)
    assert provider.sent[-1]["to"] == NG_ZAP
    assert words_of(provider.sent[-1]["data"])[0] == int(META, 16)


async def test_a_pool_without_a_zap_says_so_rather_than_guessing() -> None:
    plain = metapool()
    plain.base_pool = ""
    contract, _ = contract_for(plain)
    assert contract.zap is None
    with pytest.raises(PoolCallFailed, match="no deposit zap"):
        await contract.zap_add_liquidity([1], 0)


async def test_the_base_pool_fee_is_read_from_the_base_pool() -> None:
    contract, provider = contract_for()
    sent: list[dict] = []

    async def record(method, params=None):
        sent.append(params[0])
        return word(1_000_000)

    provider.request = record  # type: ignore[method-assign]
    assert await contract.base_fee() == 1_000_000
    assert sent[0]["to"] == RESERVES
    assert sent[0]["data"] == abi.encode_fee()


async def test_a_pool_with_no_base_pool_has_no_base_fee() -> None:
    plain = metapool()
    plain.base_pool = ""
    contract, _ = contract_for(plain)
    assert await contract.base_fee() == 0


# -- the deposit panel -----------------------------------------------------


def deposit_tab(pool: Pool | None = None, provider: FakeProvider | None = None):
    from ui.actions import DepositTab

    pool = pool or metapool()
    provider = provider or FakeProvider({"0xf558454d": word(10**18)})
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = DepositTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    return tab, provider


def test_a_plain_pool_shows_no_route_picker() -> None:
    plain = metapool()
    plain.base_pool = ""
    tab, _ = deposit_tab(plain)
    assert tab.route.visible is False
    assert list(tab.routes) == ["pool"]


def test_a_metapool_offers_both_routes() -> None:
    tab, _ = deposit_tab()
    assert tab.route.visible is True
    # Two fields on the pool route (the contract's own coins), three on the
    # underlying one (the base LP replaced by what it is made of).
    assert [f.label for f in tab.routes["pool"].fields] == ["C0", "C1"]
    assert [f.label for f in tab.routes["underlying"].fields] == ["C0", "C2", "C3"]


def test_the_underlying_route_is_the_default_where_there_is_one() -> None:
    """It is the one denominated in coins people actually hold. Holding the
    base pool's LP token is the specialist case."""
    tab, _ = deposit_tab()
    assert tab.route.value == "underlying"
    assert tab.underlying is True
    assert tab.fields is tab.routes["underlying"].fields
    assert tab.routes["underlying"].control.visible is True
    assert tab.routes["pool"].control.visible is False
    # And it reads first, because a default buried second is a default
    # nobody sees.
    assert [radio.value for radio in tab.route.content.controls] == [
        "underlying",
        "pool",
    ]


def test_a_pool_without_a_zap_stays_on_its_own_coins() -> None:
    plain = metapool()
    plain.base_pool = ""
    tab, _ = deposit_tab(plain)
    assert tab.route.value == "pool"
    assert tab.underlying is False


def test_switching_route_swaps_which_fields_are_live_and_shown() -> None:
    tab, _ = deposit_tab()

    tab.route.value = "pool"
    tab._route_changed(None)

    assert tab.underlying is False
    assert tab.fields is tab.routes["pool"].fields
    assert tab.routes["pool"].control.visible is True
    assert tab.routes["underlying"].control.visible is False


def test_an_amount_typed_on_one_route_is_not_lost_by_looking_at_the_other() -> None:
    tab, _ = deposit_tab()
    tab.routes["underlying"].fields[0].value = "5"
    tab.route.value = "pool"
    tab._route_changed(None)
    tab.route.value = "underlying"
    tab._route_changed(None)
    assert tab.routes["underlying"].fields[0].value == "5"


async def test_the_underlying_route_deposits_through_the_zap() -> None:
    tab, provider = deposit_tab()
    tab.route.value = "underlying"
    tab.slippage.value = "1"
    tab.fields[1].value = "1000"  # the first base-pool coin, 6 decimals

    await tab.submit(tab.get_contract())

    sent = provider.sent[-1]
    assert sent["to"] == NG_ZAP
    pool_word, offset, min_mint, length, *amounts = words_of(sent["data"])
    assert pool_word == int(META, 16)
    assert length == 3
    assert amounts == [0, 1000 * 10**6, 0]
    assert min_mint == abi.apply_slippage(10**18, 1.0)


async def test_the_pool_route_still_deposits_into_the_pool() -> None:
    tab, provider = deposit_tab()
    tab.route.value = "pool"
    tab._route_changed(None)
    tab.fields[0].value = "1000"
    await tab.submit(tab.get_contract())
    assert provider.sent[-1]["to"] == META
    assert provider.sent[-1]["data"][:10] == "0x" + abi.selector(
        "add_liquidity(uint256[],uint256)"
    )


async def test_the_underlying_route_approves_the_zap_not_the_pool() -> None:
    """The spender is whoever moves the coins, and on this route that is
    the zap. Approving the pool would leave the deposit reverting."""
    tab, _ = deposit_tab()
    tab.fields[1].value = "1000"

    pending = await tab.approval_needed(tab.get_contract())

    assert pending is not None
    _token, spender, amount = pending
    assert spender == NG_ZAP
    assert amount == 1000 * 10**6
    assert tab.approve_button.content == "1. Approve C2"


async def test_the_pool_route_approves_the_pool() -> None:
    tab, _ = deposit_tab()
    tab.route.value = "pool"
    tab._route_changed(None)
    tab.fields[0].value = "1000"
    pending = await tab.approval_needed(tab.get_contract())
    assert pending is not None and pending[1] == META


async def test_nothing_is_approved_while_the_quote_is_failing() -> None:
    """An allowance outlives the mistake that caused it. If the zap this
    app has the address of will not quote, it does not get approved --
    which is the whole guard against a stale or wrong registry entry."""
    tab, provider = deposit_tab()
    tab.fields[1].value = "1000"
    from wallet.base import RpcError

    provider.raise_on_call = RpcError(-32000, "execution reverted")

    await tab.refresh()

    assert tab._quote_ok is False
    assert await tab.approval_needed(tab.get_contract()) is None
    assert tab.approve_button.visible is False
    assert tab.submit_button.disabled is True


async def test_the_zap_slippage_covers_both_pools_fees() -> None:
    """A zap deposit pays the base pool's fee on the way in and the
    metapool's after it, so the suggestion is their sum plus the drift
    constant -- measured on a fork at 0.045% against 0.055% allowed."""
    from ui.actions import QUOTE_DRIFT

    tab, provider = deposit_tab()
    provider.answers["0xddca3f43"] = word(4_000_000)  # 0.04% on both pools

    await tab.suggest_slippage(tab.get_contract())

    assert float(tab.slippage.value) == pytest.approx(0.04 + 0.04 + QUOTE_DRIFT)


async def test_the_pool_route_charges_one_fee() -> None:
    tab, provider = deposit_tab()
    provider.answers["0xddca3f43"] = word(4_000_000)
    tab.route.value = "pool"
    await tab.suggest_slippage(tab.get_contract())
    from ui.actions import QUOTE_DRIFT

    assert float(tab.slippage.value) == pytest.approx(0.04 + QUOTE_DRIFT)


async def test_switching_route_re_reads_the_fee() -> None:
    """The two routes have different fees, so the suggestion has to be
    invalidated by the switch rather than cached for the pool."""
    tab, provider = deposit_tab()
    provider.answers["0xddca3f43"] = word(4_000_000)
    await tab.suggest_slippage(tab.get_contract())
    two_fees = tab.slippage.value

    tab.route.value = "pool"
    await tab.suggest_slippage(tab.get_contract())

    assert tab.slippage.value != two_fees


# -- the withdraw panel ----------------------------------------------------


def withdraw_tab(pool: Pool | None = None, provider: FakeProvider | None = None):
    from ui.actions import WithdrawTab

    pool = pool or metapool()
    provider = provider or FakeProvider({"0x41b028f3": word(500 * 10**6)})
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = WithdrawTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    return tab, provider


def test_the_receive_list_is_the_underlying_coins_by_default() -> None:
    tab, _ = withdraw_tab()
    assert tab.route.value == "underlying"
    assert [o.text for o in tab.coin_picker.options] == ["C0", "C2", "C3"]
    # The single-coin rule applies from the start, not from the first time
    # the switch is touched.
    assert tab.mode.value == "one"
    assert tab.balanced_radio.disabled is True

    tab.route.value = "pool"
    tab._route_changed(None)

    assert [o.text for o in tab.coin_picker.options] == ["C0", "C1"]


def test_the_zap_route_withdraws_into_one_coin_only() -> None:
    """There is nothing to floor a balanced zap withdrawal against: the
    base pool's reserves are not on the metapool, and a zero floor is no
    floor. So the option is greyed rather than silently unprotected."""
    tab, _ = withdraw_tab()

    assert tab.mode.value == "one"
    assert tab.balanced_radio.disabled is True
    assert "one coin" in (tab.balanced_radio.tooltip or "")

    tab.route.value = "pool"
    tab._route_changed(None)
    assert tab.balanced_radio.disabled is False


async def test_the_zap_route_needs_the_lp_approved_to_the_zap() -> None:
    """Burning at the pool needs no approval -- it burns your own balance
    -- but a zap has to be allowed to take the LP first."""
    tab, _ = withdraw_tab()
    tab.amount.value = "10"

    pending = await tab.approval_needed(tab.get_contract())

    assert pending is not None
    token, spender, amount = pending
    assert token == META  # this pool is its own LP token
    assert spender == NG_ZAP
    assert amount == 10 * 10**18
    assert tab.approve_button.content == "1. Approve LP"


async def test_the_pool_route_needs_no_approval() -> None:
    tab, _ = withdraw_tab()
    tab.route.value = "pool"
    tab._route_changed(None)
    tab.amount.value = "10"
    assert await tab.approval_needed(tab.get_contract()) is None


async def test_the_zap_withdrawal_is_sent_to_the_zap() -> None:
    tab, provider = withdraw_tab()
    tab.coin_picker.value = "2"
    tab.amount.value = "10"
    tab.slippage.value = "1"

    await tab.submit(tab.get_contract())

    sent = provider.sent[-1]
    assert sent["to"] == NG_ZAP
    assert sent["data"][:10] == "0x" + abi.selector(
        "remove_liquidity_one_coin(address,uint256,int128,uint256)"
    )
    pool_word, burn, index, floor = words_of(sent["data"])
    assert pool_word == int(META, 16)
    assert burn == 10 * 10**18
    assert index == 2
    assert floor == abi.apply_slippage(500 * 10**6, 1.0)


async def test_the_pool_route_still_burns_at_the_pool() -> None:
    tab, provider = withdraw_tab()
    tab.route.value = "pool"
    tab._route_changed(None)
    tab.mode.value = "one"
    tab.amount.value = "10"
    await tab.submit(tab.get_contract())
    assert provider.sent[-1]["to"] == META


def test_a_pool_without_a_zap_hides_the_withdraw_route_picker() -> None:
    plain = metapool()
    plain.base_pool = ""
    tab, _ = withdraw_tab(plain)
    assert tab.route.visible is False
    assert tab.underlying is False
