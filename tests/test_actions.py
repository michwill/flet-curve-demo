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


def make_pool() -> Pool:
    pool = Pool.from_v2(
        {
            "address": POOL_ADDRESS,
            "pool_type": "crvusd",
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
    from ui.actions import DepositTab, StakeTab, SwapTab, WithdrawTab

    pool = make_pool()
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


def test_staking_offers_no_slippage() -> None:
    """There is no rate to protect: LP tokens go into the gauge one for one."""
    deposit, _withdraw, _swap, stake = tabs()
    assert deposit.slippage in fields_of(deposit.mount())
    assert stake.slippage not in fields_of(stake.mount())
