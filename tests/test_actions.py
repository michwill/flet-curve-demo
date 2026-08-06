"""The action panels' calldata, without a running page.

Only `submit()` is exercised here: it is the one path that turns what the
user typed into a transaction, and the one place where a mistake costs real
money rather than a redraw. The panels build their controls in `__init__`,
which needs no page, so they can be constructed outright -- `page` is only
touched when a status line is updated.

The balanced withdrawal is the interesting case. Its `min_amounts` floor is
each reserve's share of the LP supply, which means two numbers that come
from different places (the API's reserves, the chain's supply) and used to
be conflated: an earlier version divided by the sum of the reserves, which
is not the LP supply at all.
"""

from __future__ import annotations

import pytest

from curve import abi
from curve.models import Pool
from curve.pool import PoolContract
import flet as ft

from ui.actions import WithdrawTab
from ui.typography import BODY
from wallet.base import RpcError, WalletProvider

ACCOUNT = "0x1111111111111111111111111111111111111111"
POOL_ADDRESS = "0x390f3595bCa2df7D23783DFd126427CCeb997BF4"
LP_TOKEN = "0x6c3F90f043a72FA612cbac8115EE7e52BDe6E490"

#: Reserves, in human numbers, as the API reports them.
USDT_RESERVE = 1_000_000.0
CRVUSD_RESERVE = 2_000_000.0
#: LP tokens outstanding. Deliberately not the sum of the reserves.
LP_SUPPLY = 1_500_000 * 10**18


def word(value: int) -> str:
    return "0x" + f"{value:064x}"


class FakeProvider(WalletProvider):
    """Answers `eth_call` by selector and records what was sent."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.sent: list[dict] = []
        self.default = word(0)
        self.raise_on_call: Exception | None = None

    async def request(self, method: str, params=None):
        params = params or []
        if method == "eth_call":
            if self.raise_on_call is not None:
                raise self.raise_on_call
            return self.answers.get(params[0]["data"][:10], self.default)
        if method == "eth_sendTransaction":
            self.sent.append(params[0])
            return "0x" + "cd" * 32
        raise AssertionError(f"unexpected method {method}")


class StubPage:
    """Enough of `ft.Page` for a panel that never redraws."""

    def update(self) -> None:
        pass

    def run_task(self, *_args, **_kwargs) -> None:
        pass


def make_pool(registry: str = "crvusd") -> Pool:
    pool = Pool.from_v2(
        {
            "address": POOL_ADDRESS,
            "pool_type": registry,
            "lp_token_address": LP_TOKEN,
            "gauges": [],
            "coins": [
                {"symbol": "USDT", "address": "0x" + "aa" * 20, "decimals": 6},
                {"symbol": "crvUSD", "address": "0x" + "bb" * 20, "decimals": 18},
            ],
        }
    )
    return pool.merge_detail(
        {
            "n_coins": 2,
            "balances": [USDT_RESERVE, CRVUSD_RESERVE],
            "coins": [
                {"symbol": "USDT", "address": "0x" + "aa" * 20, "decimals": 6},
                {"symbol": "crvUSD", "address": "0x" + "bb" * 20, "decimals": 18},
            ],
        }
    )


def make_tab(provider: FakeProvider) -> WithdrawTab:
    pool = make_pool()
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = WithdrawTab(StubPage(), pool, lambda: contract, None)
    tab.slippage.value = "1"
    return tab


def words_of(data: str) -> list[int]:
    """The 32-byte words of some calldata, selector stripped."""
    body = data[10:]
    return [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]


async def test_balanced_withdrawal_floors_each_coin_at_its_share() -> None:
    provider = FakeProvider({"0x18160ddd": word(LP_SUPPLY)})  # totalSupply()
    tab = make_tab(provider)
    tab.amount.value = "150000"  # a tenth of the supply

    contract = tab.get_contract()
    await tab.submit(contract)

    # remove_liquidity(uint256,uint256[2]): amount, then one word per coin.
    amount, usdt_min, crvusd_min = words_of(provider.sent[-1]["data"])
    assert amount == 150_000 * 10**18
    # A tenth of the pool, less the 1% tolerance, in each coin's own units.
    assert usdt_min == pytest.approx(100_000 * 10**6 * 0.99, rel=1e-9)
    assert crvusd_min == pytest.approx(200_000 * 10**18 * 0.99, rel=1e-9)


async def test_a_zero_floor_is_sent_when_the_supply_cannot_be_read() -> None:
    """No protection is better than a transaction that cannot be built.

    The floor is a safety margin, not a requirement: a node that will not
    answer `totalSupply()` should not stop someone withdrawing.
    """
    provider = FakeProvider()
    provider.raise_on_call = RpcError(-32000, "execution reverted")
    tab = make_tab(provider)
    tab.amount.value = "1"

    await tab.submit(tab.get_contract())
    _amount, *floors = words_of(provider.sent[-1]["data"])
    assert floors == [0, 0]


async def test_the_supply_read_targets_the_lp_token() -> None:
    """Not the pool: on older pools they are different contracts."""
    provider = FakeProvider({"0x18160ddd": word(LP_SUPPLY)})
    seen: list[str] = []

    original = provider.request

    async def spy(method, params=None):
        if method == "eth_call":
            seen.append(params[0]["to"])
        return await original(method, params)

    provider.request = spy  # type: ignore[method-assign]
    tab = make_tab(provider)
    tab.amount.value = "1"
    await tab.submit(tab.get_contract())
    assert LP_TOKEN in seen


async def test_withdrawing_nothing_is_refused_before_any_call() -> None:
    provider = FakeProvider()
    tab = make_tab(provider)
    tab.amount.value = ""
    with pytest.raises(Exception, match="Enter an amount"):
        await tab.submit(tab.get_contract())
    assert provider.sent == []


async def test_a_one_coin_withdrawal_floors_at_the_pool_s_own_quote() -> None:
    """The floor comes from `calc_withdraw_one_coin`, not from the reserves.

    The pool knows what an imbalanced withdrawal costs; this app does not,
    and guessing would be a second implementation of the invariant.
    """
    quote = "0x" + abi.selector("calc_withdraw_one_coin(uint256,int128)")
    provider = FakeProvider({quote: word(99 * 10**6)})
    tab = make_tab(provider)
    tab.amount.value = "100"
    tab.mode.value = "one"
    tab.coin_picker.value = "0"

    await tab.submit(tab.get_contract())
    sent = provider.sent[-1]["data"]
    assert sent.startswith(
        "0x" + abi.selector("remove_liquidity_one_coin(uint256,int128,uint256)")
    )
    amount, index, minimum = words_of(sent)
    assert (amount, index) == (100 * 10**18, 0)
    assert minimum == pytest.approx(99 * 10**6 * 0.99, rel=1e-9)


# -- how the panel is laid out ---------------------------------------------
#
# The amounts are the subject of these panels, so they get the panel's full
# width. They used to stop short of the right edge -- Material gives a text
# field its own idea of a sensible width unless the column stretches it --
# which read as if they were dodging the slippage box beside them.


def tabs():
    """The four panels, on a pool with a gauge.

    With no gauge the Stake panel is a sentence explaining why there is
    nothing to stake, which is right but has no fields to lay out.
    """
    from ui.actions import DepositTab, StakeTab, SwapTab, WithdrawTab

    pool = make_pool()
    pool.gauge = "0x" + "cc" * 20
    page = StubPage()
    return [
        cls(page, pool, lambda: None, None)
        for cls in (DepositTab, WithdrawTab, SwapTab, StakeTab)
    ]


def widths_are_stretched(column) -> bool:
    import flet as ft

    return column.horizontal_alignment == ft.CrossAxisAlignment.STRETCH


def fields_of(control) -> list:
    """Every text field in a control tree."""
    import flet as ft

    found = []
    if isinstance(control, ft.TextField):
        found.append(control)
    for child in getattr(control, "controls", None) or []:
        found += fields_of(child)
    inner = getattr(control, "content", None)
    if isinstance(inner, ft.Control):
        found += fields_of(inner)
    return found


@pytest.mark.parametrize("index", range(4))
def test_every_panel_stretches_its_fields(index: int) -> None:
    tab = tabs()[index]
    assert widths_are_stretched(tab.mount())


def test_a_field_and_its_balance_line_stretch_together() -> None:
    """The inner column has to stretch too, or the field inside it keeps
    its intrinsic width however wide the panel is."""
    tab = tabs()[0]
    tab.mount()
    pairs = [c for c in tab.control.controls if isinstance(c, ft.Column)]
    assert pairs and all(widths_are_stretched(pair) for pair in pairs)


def test_no_amount_field_sets_its_own_width() -> None:
    """A fixed width would defeat the stretch. Slippage is the exception --
    it is deliberately small."""
    for tab in tabs():
        for field in fields_of(tab.mount()):
            if field is tab.slippage:
                continue
            assert field.width is None, f"{tab.title}: {field.label} pins its width"


def test_slippage_is_small_and_out_of_the_way() -> None:
    tab = tabs()[0]
    tab.mount()
    assert tab.slippage.width and tab.slippage.width <= 100
    assert tab.slippage.text_size and tab.slippage.text_size < BODY
    row = next(
        r
        for r in tab.control.controls
        if isinstance(r, ft.Row) and tab.slippage in (r.controls or [])
    )
    assert row.alignment == ft.MainAxisAlignment.END


def test_slippage_sits_with_the_amounts_not_with_the_button() -> None:
    """Against the submit button it reads as part of the action rather
    than as a setting for it."""
    for tab in tabs():
        if not tab.uses_slippage:
            continue
        controls = tab.mount().controls
        slippage_at = next(
            i
            for i, c in enumerate(controls)
            if isinstance(c, ft.Row) and tab.slippage in (c.controls or [])
        )
        assert slippage_at < controls.index(tab.submit_button) - 1
        assert slippage_at < controls.index(tab.estimate)


# -- filling a field with everything you have ------------------------------


def max_button(field: ft.TextField) -> ft.TextButton | None:
    return field.suffix_icon if isinstance(field.suffix_icon, ft.TextButton) else None


def test_every_amount_field_carries_its_own_max() -> None:
    """It used to be a button underneath, which said nothing about *which*
    amount it filled -- and on Deposit there is one per coin."""
    for tab in tabs():
        amounts = [f for f in fields_of(tab.mount()) if f is not tab.slippage]
        assert amounts, f"{tab.title}: no amount field"
        for field in amounts:
            assert max_button(field), f"{tab.title}: {field.label} has no MAX"


def test_no_max_button_is_left_loose_in_the_panel() -> None:
    def loose_buttons(control) -> list:
        found = []
        if isinstance(control, ft.TextButton) and control.content == "MAX":
            found.append(control)
        for child in getattr(control, "controls", None) or []:
            found += loose_buttons(child)
        return found

    for tab in tabs():
        assert not loose_buttons(tab.mount()), f"{tab.title}: MAX outside its field"


def test_deposit_max_fills_that_coin_with_the_whole_balance() -> None:
    tab = tabs()[0]
    tab.mount()
    tab.balances = [1_500_000, 2 * 10**18]  # 1.5 USDT (6dp), 2 crvUSD (18dp)

    max_button(tab.fields[0]).on_click(None)
    assert tab.fields[0].value == "1.5"
    assert tab.fields[1].value in (None, "")

    max_button(tab.fields[1]).on_click(None)
    assert tab.fields[1].value == "2"


def test_max_keeps_every_decimal_the_token_has() -> None:
    """The balance line is rounded for reading; the field becomes calldata,
    and a rounded number would leave dust behind or exceed the balance."""
    tab = tabs()[0]
    tab.mount()
    tab.balances = [1_234_567, 0]  # 1.234567 USDT, more places than shown
    max_button(tab.fields[0]).on_click(None)
    assert tab.fields[0].value == "1.234567"


def test_swap_max_follows_the_coin_being_sold() -> None:
    _deposit, _withdraw, swap, _stake = tabs()
    swap.mount()
    swap.balance = 3 * 10**6  # 3 USDT, the first coin
    max_button(swap.amount).on_click(None)
    assert swap.amount.value == "3"

    swap.from_coin.value = "1"  # crvUSD, 18 decimals
    swap.balance = 4 * 10**18
    max_button(swap.amount).on_click(None)
    assert swap.amount.value == "4"


def test_stake_max_follows_the_direction() -> None:
    """Staking offers the wallet balance; unstaking offers what is staked."""
    stake = tabs()[3]
    stake.mount()
    stake.lp_balance, stake.staked = 5 * 10**18, 7 * 10**18

    max_button(stake.amount).on_click(None)
    assert stake.amount.value == "5"

    stake.direction.value = "unstake"
    max_button(stake.amount).on_click(None)
    assert stake.amount.value == "7"


def test_staking_offers_no_slippage() -> None:
    """There is no rate to protect: LP tokens go into the gauge one for one."""
    deposit, _withdraw, _swap, stake = tabs()
    assert deposit.slippage in fields_of(deposit.mount())
    assert stake.slippage not in fields_of(stake.mount())


# -- slippage from the pool's own fee --------------------------------------
#
# A fixed 0.5% is arbitrary: it is loose for a stable pool charging 0.01%
# and no better than a guess for a crypto pool charging 1.5%. The fee is
# what the trade is expected to cost, so it is the scale the tolerance
# belongs on.


class FeeProvider(FakeProvider):
    """Answers `fee()` and, optionally, `dynamic_fee(i, j)`."""

    def __init__(self, flat: int, pair: int | None = None) -> None:
        super().__init__()
        self.flat, self.pair = flat, pair
        self.reads: list[str] = []

    async def request(self, method: str, params=None):
        if method != "eth_call":
            return await super().request(method, params)
        data = (params or [{}])[0].get("data", "")
        self.reads.append(data[:10])
        if data.startswith("0x" + abi.selector("fee()")):
            return word(self.flat)
        if data.startswith("0x" + abi.selector("dynamic_fee(int128,int128)")):
            return word(self.pair) if self.pair is not None else "0x"
        return word(0)


def tab_with_fee(cls, flat: int, pair: int | None = None, registry: str = "crvusd"):
    pool = make_pool(registry=registry)
    pool.gauge = "0x" + "cc" * 20
    provider = FeeProvider(flat, pair)
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = cls(StubPage(), pool, lambda: contract, None)
    tab.mount()
    return tab, provider


async def test_deposit_slippage_is_the_fee_plus_a_little() -> None:
    """The whole rule: a deposit may lose its pool's fee, plus 0.005% for
    the quote going stale. 3pool charges 0.015%, so 0.02%."""
    from ui.actions import DepositTab

    tab, _ = tab_with_fee(DepositTab, 1_500_000, registry="main")
    await tab.refresh()
    assert float(tab.slippage.value) == pytest.approx(0.02)


async def test_the_same_line_holds_whatever_the_implementation() -> None:
    """One rule, no per-registry branch: the modern pools mint exactly the
    quote, so a whole fee is margin they do not need but does not hurt."""
    from ui.actions import DepositTab, ESTIMATE_FEE_SHARE, QUOTE_DRIFT, slippage_for

    for registry in ("main", "factory", "crvusd", "stableswapng", "twocryptong", "new-2027"):
        tab, _ = tab_with_fee(DepositTab, 1_000_000, registry=registry)
        await tab.refresh()
        expected = slippage_for(1_000_000, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)
        assert float(tab.slippage.value) == pytest.approx(expected), registry


async def test_a_tiny_fee_still_gets_the_drift_allowance() -> None:
    """Strategic USD Reserves charges 0.001%, so the fee term is nothing
    and the constant is the whole allowance -- not a floor bolted on, just
    `a * fee + b` with a small fee."""
    from ui.actions import DepositTab, QUOTE_DRIFT

    tab, _ = tab_with_fee(DepositTab, 100_000, registry="stableswapng")
    await tab.refresh()
    assert float(tab.slippage.value) == pytest.approx(0.001 + QUOTE_DRIFT)


async def test_withdrawing_uses_the_flat_fee_too() -> None:
    from ui.actions import WithdrawTab

    tab, provider = tab_with_fee(WithdrawTab, 1_000_000, pair=9_999_999)
    await tab.refresh()
    assert float(tab.slippage.value) == pytest.approx(0.01 + 0.005)
    assert "0x" + abi.selector("dynamic_fee(int128,int128)") not in provider.reads


async def test_swapping_stays_tight_because_its_quote_is_exact() -> None:
    """`get_dy` is the same maths the swap runs, fee included, so there is
    no estimator error to give back -- a fifth of the fee, not twice."""
    from ui.actions import SwapTab

    tab, _ = tab_with_fee(SwapTab, 1_000_000, pair=2_000_000)
    await tab.refresh()
    assert tab.slippage.value == "0.004"  # from the pair fee, not the flat one


async def test_a_swap_pool_without_dynamic_fee_falls_back() -> None:
    from ui.actions import SwapTab

    tab, _ = tab_with_fee(SwapTab, 4_577_514)  # no pair fee
    await tab.refresh()
    assert tab.slippage.value == "0.00916"


async def test_a_deposit_is_always_given_more_room_than_a_swap() -> None:
    """The difference is not a preference; it is that one quote is exact."""
    from ui.actions import DepositTab, SwapTab

    for registry in ("main", "crvusd", "stableswapng", "factory_tricrypto"):
        deposit, _ = tab_with_fee(DepositTab, 1_000_000, registry=registry)
        swap, _ = tab_with_fee(SwapTab, 1_000_000, registry=registry)
        await deposit.refresh()
        await swap.refresh()
        assert float(deposit.slippage.value) > float(swap.slippage.value), registry


async def test_changing_the_pair_re_reads_the_fee() -> None:
    from ui.actions import SwapTab

    tab, provider = tab_with_fee(SwapTab, 1_000_000, pair=2_000_000)
    await tab.refresh()
    first = provider.reads.count("0x" + abi.selector("dynamic_fee(int128,int128)"))

    tab.to_coin.value = "0"
    await tab.refresh()
    assert provider.reads.count("0x" + abi.selector("dynamic_fee(int128,int128)")) > first


async def test_the_fee_is_read_once_per_pair_not_per_keystroke() -> None:
    from ui.actions import DepositTab

    tab, provider = tab_with_fee(DepositTab, 1_500_000)
    for _ in range(4):
        await tab.refresh()
    assert provider.reads.count("0x" + abi.selector("fee()")) == 1


async def test_what_the_user_typed_is_never_overwritten() -> None:
    from ui.actions import DepositTab

    tab, _ = tab_with_fee(DepositTab, 1_500_000)
    tab.slippage.value = "1.5"
    tab._slippage_edited(None)  # the field's own on_change

    await tab.refresh()
    assert tab.slippage.value == "1.5"


async def test_a_pool_that_will_not_answer_keeps_the_default() -> None:
    """Better a workable default than an empty box."""
    from ui.actions import DepositTab
    from ui.actions import DEFAULT_SLIPPAGE

    tab, _ = tab_with_fee(DepositTab, 0)  # fee() answers zero
    await tab.refresh()
    assert tab.slippage.value == str(DEFAULT_SLIPPAGE)


async def test_staking_reads_no_fee_at_all() -> None:
    """It has no slippage field, so there is nothing to suggest."""
    from ui.actions import StakeTab

    tab, provider = tab_with_fee(StakeTab, 1_500_000)
    await tab.refresh()
    assert "0x" + abi.selector("fee()") not in provider.reads


async def test_the_line_covers_every_measured_pool() -> None:
    """The data the constants were fitted to, kept as a regression.

    Each row is one mainnet pool: its fee, and the tolerance its deposit
    actually needed -- bisected on a fork until `add_liquidity` stopped
    reverting, plus what the quote lost by being up to five blocks stale.
    The line has to sit above every one of them.
    """
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, slippage_for

    measured = [                      # (fee in 1e10 units, needed %)
    (  15_000_000,  0.13696),   # cvxCrv/Crv             factory
    (   1_500_000,  0.00952),   # DAI/USDC/USDT          main
    (   4_000_000,  0.00537),   # alETHfrxETH            factory
    (   4_000_000,  0.00220),   # msETH/WETH             factory
    (     207_000,  0.00035),   # TricryptoUSDT          factory_tricrypto
    (  10_000_000,  0.00013),   # USDS/stUSDS            stableswapng
    (   1_000_000,  0.00012),   # DOLA/sUSDe             stableswapng
    (   1_000_000,  0.00012),   # AUSD/USDC              stableswapng
    (   4_000_000,  0.00012),   # BOLD/USDC Pool         stableswapng
    (   1_000_000,  0.00012),   # FRAX/frxUSD            stableswapng
    (   1_000_000,  0.00012),   # NUSD/USDC              stableswapng
    (   1_000_000,  0.00012),   # PayPool                stableswapng
    (   2_000_000,  0.00012),   # RLUSD/USDC             stableswapng
    (   3_000_000,  0.00012),   # TricryptoUSDC          factory_tricrypto
    (   4_000_000,  0.00012),   # USD0/USD0++            stableswapng
    (   1_000_000,  0.00012),   # USDC/USDat             stableswapng
    (   1_000_000,  0.00012),   # USDC/fxUSD             stableswapng
    (   1_000_000,  0.00012),   # USDG/USDC              stableswapng
    (   1_000_000,  0.00012),   # USDtb-USDC             stableswapng
    (  60_000_000,  0.00012),   # YB WETH                twocryptong
    ( 100_000_000,  0.00012),   # YB cbBTC               twocryptong
    ( 100_000_000,  0.00012),   # YB tBTC                twocryptong
    (  20_000_000,  0.00012),   # apxUSD-USDC v3         stableswapng
    (   1_000_000,  0.00012),   # crvUSD/frxUSD          stableswapng
    (   4_000_000,  0.00012),   # frxUSD/msUSD           stableswapng
    (   1_000_000,  0.00012),   # frxUSD/trUSD           stableswapng
    (   1_000_000,  0.00012),   # sfrxUSD/frxUSD         stableswapng
    (   1_000_000,  0.00012),   # strUSD/trUSD           stableswapng
    (   1_000_000,  0.00012),   # tBTC/WBTC              crvusd
    (   1_000_000,  0.00012),   # trUSD/USDC             stableswapng
    (   2_000_000,  0.00000),   # sDAI/sUSDe             stableswapng
    (   1_000_000,  0.00000),   # DOLA/sUSDS             stableswapng
    (   2_000_000,  0.00000),   # reUSD/scrvUSD          stableswapng
    (   2_000_000,  0.00000),   # ETH+/ETH               stableswapng
    (   1_000_000,  0.00000),   # FRAXUSDe               stableswapng
    (     100_000,  0.00000),   # Strategic USD Reserv   stableswapng
    (   1_000_000,  0.00000),   # TricryptoLLAMA         factory_tricrypto
    (           0,  0.00000),   # USAT/USDT              stableswapng
    (   3_000_000,  0.00000),   # USD-BTC-ETH            crypto
    ( 100_000_000,  0.00000),   # YB WBTC                twocryptong
    (  20_000_000,  0.00000),   # apyUSD-apxUSD          stableswapng
    (   1_000_000,  0.00000),   # crvUSD/USDC            crvusd
    (   1_000_000,  0.00000),   # crvUSD/USDT            crvusd
    (   2_000_000,  0.00000),   # osETH/rETH             stableswapng
    ]
    for fee, needed in measured:
        allowed = slippage_for(fee, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)
        assert allowed >= needed, f"fee {fee}: allows {allowed}, needs {needed}"


def test_the_slope_is_one_whole_fee() -> None:
    """Not a fitted constant but a ceiling: the imbalance fee on a fully
    single-sided deposit approaches the base fee as a two-coin pool skews,
    so no pool of that shape can need more. The measured worst, cvxCrv/Crv,
    was 0.91x -- under it, as it must be."""
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, slippage_for

    assert ESTIMATE_FEE_SHARE == 1.0
    binding_fee, binding_need = 15_000_000, 0.13696      # cvxCrv/Crv
    assert binding_need / (binding_fee / 10**10 * 100) < ESTIMATE_FEE_SHARE
    assert slippage_for(binding_fee, ESTIMATE_FEE_SHARE, QUOTE_DRIFT) >= binding_need


def test_a_low_constant_is_what_the_slope_buys() -> None:
    """A pegged pool charging 0.001% should not be given the tolerance a
    volatile one needs; that is the point of keeping `a` above zero."""
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, slippage_for

    pegged = slippage_for(100_000, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)     # 0.001% fee
    volatile = slippage_for(60_000_000, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)  # 0.6% fee
    assert pegged < 0.01, "a pegged pool should stay well under a hundredth"
    assert volatile > 0.5, "a 0.6% fee pool needs room the constant cannot give"


def test_the_arithmetic_is_a_times_fee_plus_b() -> None:
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, SLIPPAGE_OF_FEE, slippage_for

    assert (SLIPPAGE_OF_FEE, ESTIMATE_FEE_SHARE, QUOTE_DRIFT) == (0.2, 1.0, 0.005)
    # 1e10 is 100%, so 10_000_000 is 0.1%.
    assert slippage_for(10_000_000) == pytest.approx(0.02)
    assert slippage_for(0) == 0
    assert slippage_for(10_000_000, ESTIMATE_FEE_SHARE, QUOTE_DRIFT) == pytest.approx(0.105)
    # The constant is what carries a pool whose fee rounds to nothing.
    assert slippage_for(0, ESTIMATE_FEE_SHARE, QUOTE_DRIFT) == pytest.approx(QUOTE_DRIFT)


def test_a_deposit_allows_more_than_a_swap_at_every_fee() -> None:
    """One quote is exact and the other is not; that ordering must hold
    whatever the pool charges."""
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, SLIPPAGE_OF_FEE, slippage_for

    for fee in (0, 100_000, 1_000_000, 5_520_000, 155_010_000):
        deposit = slippage_for(fee, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)
        swap = slippage_for(fee, SLIPPAGE_OF_FEE)
        assert deposit > swap


# -- waiting for the chain -------------------------------------------------
#
# The panels used to re-read straight after broadcasting, which reads the
# state *before* the transaction: an approval landed and left the submit
# button disabled. Now they wait for the receipt, and for the endpoint to
# have caught up to the block it names.


@pytest.fixture(autouse=True)
def _no_polling_delay(monkeypatch):
    """Run the confirmation loops at full speed."""
    import ui.actions

    monkeypatch.setattr(ui.actions, "CONFIRM_INTERVAL", 0)


class MinedProvider(FakeProvider):
    """Sends, then mines at `block` after `pending` empty polls."""

    def __init__(self, block: int = 500, pending: int = 1, status: str = "0x1") -> None:
        super().__init__()
        self.block, self.pending, self.status = block, pending, status
        self.heads = [block - 2, block - 1, block]
        self.receipts_asked = 0
        self.allowance_after = 0

    async def request(self, method: str, params=None):
        if method == "eth_getTransactionReceipt":
            self.receipts_asked += 1
            if self.receipts_asked <= self.pending:
                return None
            return {"blockNumber": hex(self.block), "status": self.status}
        if method == "eth_blockNumber":
            return hex(self.heads.pop(0) if self.heads else self.block)
        if method == "eth_call":
            data = (params or [{}])[0].get("data", "")
            if data.startswith("0x" + abi.selector("allowance(address,address)")):
                # Only true once the transaction has been mined, which is
                # the whole point: reading earlier shows the old allowance.
                mined = self.receipts_asked > self.pending
                return word(10**30 if mined else 0)
            if data.startswith("0x" + abi.selector("fee()")):
                return word(1_000_000)
            return "0x"
        return await super().request(method, params)


def deposit_tab(provider):
    pool = make_pool()
    contract = PoolContract(provider, pool, ACCOUNT)
    from ui.actions import DepositTab

    tab = DepositTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    return tab


async def test_an_approval_waits_to_be_mined_before_reading_back() -> None:
    provider = MinedProvider(pending=2)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1"

    await tab._approve_clicked(None)

    assert provider.receipts_asked > 2, "did not wait for the receipt"
    assert "confirm" in tab.status.value.lower() or "Approved" in tab.status.value


async def test_the_block_the_transaction_landed_in_is_waited_for() -> None:
    """A load-balanced endpoint can answer from a node a block or two
    behind, which reads as the transaction having been rolled back."""
    provider = MinedProvider(block=500, pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1"

    await tab._approve_clicked(None)
    assert provider.heads == [], "the head was not polled until it caught up"


async def test_a_mined_revert_is_reported_not_celebrated() -> None:
    provider = MinedProvider(status="0x0")
    tab = deposit_tab(provider)
    tab.fields[0].value = "1"

    await tab._approve_clicked(None)
    assert "reverted" in tab.status.value.lower()


async def test_a_confirmed_deposit_clears_the_amounts() -> None:
    """The number that was there has been spent; leaving it invites
    sending it twice."""
    provider = MinedProvider(pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1"
    tab.balances = [10**18, 0]

    async def submit(contract):
        return "0x" + "cd" * 32

    tab.submit = submit  # type: ignore[assignment]
    tab.on_done = _noop
    await tab._submit_clicked(None)
    assert tab.fields[0].value == ""


async def _noop() -> None:
    return None


# -- what the confirmation says --------------------------------------------
#
# The block number the receipt named still governs the reads that follow it
# -- see `curve.confirm` -- but it is not what someone who just deposited
# wants read back to them. The amount they typed is.


async def test_a_confirmed_deposit_names_the_amount_not_the_block() -> None:
    provider = MinedProvider(block=21_000_000, pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1000"  # USDT, 6 decimals

    async def submit(contract):
        return "0x" + "cd" * 32

    tab.submit = submit  # type: ignore[assignment]
    tab.on_done = _noop
    await tab._submit_clicked(None)

    assert tab.status.value == "Deposited 1,000.00 USDT."
    assert "block" not in tab.status.value


async def test_a_deposit_of_several_coins_names_them_all() -> None:
    provider = MinedProvider(pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1000"
    tab.fields[1].value = "2.5"

    async def submit(contract):
        return "0x" + "cd" * 32

    tab.submit = submit  # type: ignore[assignment]
    tab.on_done = _noop
    await tab._submit_clicked(None)

    assert tab.status.value == "Deposited 1,000.00 USDT + 2.5 crvUSD."


async def test_an_approval_names_what_it_approved() -> None:
    provider = MinedProvider(pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1000"

    await tab._approve_clicked(None)

    assert tab.status.value == "Approved 1,000.00 USDT."


async def test_the_summary_is_taken_before_the_fields_are_cleared() -> None:
    """`clear_inputs` runs on success, so a summary read afterwards would
    report an empty deposit."""
    provider = MinedProvider(pending=0)
    tab = deposit_tab(provider)
    tab.fields[0].value = "7"

    async def submit(contract):
        return "0x" + "cd" * 32

    tab.submit = submit  # type: ignore[assignment]
    tab.on_done = _noop
    await tab._submit_clicked(None)

    assert tab.fields[0].value == ""
    assert "7 USDT" in tab.status.value


async def test_unstaking_says_unstaked() -> None:
    from ui.actions import StakeTab

    provider = MinedProvider(pending=0)
    pool = make_pool()
    pool.gauge = "0x" + "ee" * 20
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = StakeTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    tab.amount.value = "5"

    tab.direction.value = "unstake"
    assert tab.done_message() == "Unstaked 5 LP."
    tab.direction.value = "stake"
    assert tab.done_message() == "Staked 5 LP."


async def test_a_swap_names_both_sides() -> None:
    from ui.actions import SwapTab

    provider = MinedProvider(pending=0)
    pool = make_pool()
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = SwapTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    tab.amount.value = "250"

    assert tab.done_message() == "Swapped 250 USDT for crvUSD."


async def test_nothing_typed_leaves_a_bare_confirmation() -> None:
    """No amount is not a reason to print an empty one."""
    provider = MinedProvider(pending=0)
    tab = deposit_tab(provider)
    assert tab.done_message() == "Deposited."


def test_the_shown_tolerance_is_never_tighter_than_the_computed_one() -> None:
    """Three significant figures, rounded up: `%.3g` would turn 0.01925
    into 0.0192, and a floor shown tighter than it was calculated is a
    floor that reverts."""
    from ui.actions import ESTIMATE_FEE_SHARE, QUOTE_DRIFT, format_slippage, slippage_for

    for fee in range(0, 200_000_000, 137_017):
        exact = slippage_for(fee, ESTIMATE_FEE_SHARE, QUOTE_DRIFT)
        assert float(format_slippage(exact)) >= exact, fee


# -- what the buttons say --------------------------------------------------
#
# `ft.Button` carries its label in `content`; it has no `text` property, so
# assigning one silently sets an attribute nobody reads. Both places that
# rename a button did exactly that -- Unstake stayed labelled "Stake", and
# the "2." that pairs with "1. Approve" never appeared.


async def test_unstaking_says_unstake() -> None:
    from ui.actions import StakeTab

    tab, _ = tab_with_fee(StakeTab, 1_000_000)
    tab.pool.gauge = "0x" + "cc" * 20
    await tab.refresh()
    assert tab.submit_button.content == "Stake"

    tab.direction.value = "unstake"
    await tab.refresh()
    assert tab.submit_button.content == "Unstake"

    tab.direction.value = "stake"
    await tab.refresh()
    assert tab.submit_button.content == "Stake"


async def test_the_submit_button_is_numbered_while_an_approval_is_pending() -> None:
    """"1. Approve" then "2. Deposit" -- the second half never showed."""
    from ui.actions import DepositTab

    provider = FeeProvider(1_000_000)
    pool = make_pool()
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = DepositTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    tab.fields[0].value = "1"

    await tab._sync_approval(contract)      # allowance is zero in the fake
    assert tab.approve_button.content == "1. Approve USDT"
    assert tab.submit_button.content == "2. Deposit"


async def test_and_loses_the_number_once_it_is_approved() -> None:
    from ui.actions import DepositTab

    class Approved(FeeProvider):
        async def request(self, method, params=None):
            data = (params or [{}])[0].get("data", "") if method == "eth_call" else ""
            if data.startswith("0x" + abi.selector("allowance(address,address)")):
                return word(10**30)
            return await super().request(method, params)

    provider = Approved(1_000_000)
    pool = make_pool()
    contract = PoolContract(provider, pool, ACCOUNT)
    tab = DepositTab(StubPage(), pool, lambda: contract, None)
    tab.mount()
    tab.fields[0].value = "1"

    await tab._sync_approval(contract)
    assert tab.submit_button.content == "Deposit"
    assert not tab.approve_button.visible


# -- the status line -------------------------------------------------------
#
# A wallet takes seconds to answer and a block takes twelve. A line of text
# that just sits there is indistinguishable from one that has stopped, so
# the waiting states spin and the panel is tinted by what it is saying.


def status_of(tab):
    return tab.status.value, tab.status_spinner.visible, tab.status_panel.bgcolor


async def test_waiting_states_spin() -> None:
    from ui.actions import DepositTab

    provider = MinedProvider(pending=2)
    tab = deposit_tab(provider)
    tab.fields[0].value = "1"

    seen = []
    say = tab._say

    def record(message, colour=None, *, pending=False):
        seen.append((message, pending))
        say(message, colour, pending=pending)

    tab._say = record
    await tab._approve_clicked(None)

    assert any(pending for _m, pending in seen), "nothing ever showed as pending"
    waiting = [m for m, pending in seen if pending]
    assert any("wallet" in m for m in waiting)
    assert any("confirm" in m for m in waiting)


async def test_the_panel_hides_when_there_is_nothing_to_say() -> None:
    from ui.actions import DepositTab

    tab, _ = tab_with_fee(DepositTab, 1_000_000)
    assert not tab.status_panel.visible

    tab._say("something")
    assert tab.status_panel.visible
    tab._say("")
    assert not tab.status_panel.visible


def test_each_kind_of_status_gets_its_own_tint() -> None:
    """Told apart before the words are read."""
    from ui.actions import DepositTab

    tab, _ = tab_with_fee(DepositTab, 1_000_000)
    tints = {}
    for label, colour, pending in [
        ("pending", None, True),
        ("failed", ft.Colors.ERROR, False),
        ("done", ft.Colors.GREEN_600, False),
        ("plain", None, False),
    ]:
        tab._say(label, colour, pending=pending)
        tints[label] = tab.status_panel.bgcolor
    assert len(set(tints.values())) == 4, tints
    tab._say("pending", None, pending=True)
    assert tab.status_spinner.visible
    tab._say("done", ft.Colors.GREEN_600)
    assert not tab.status_spinner.visible
