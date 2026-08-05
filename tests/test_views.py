"""Building every view off-screen, with no app and no display.

These are cheap and they earn their place: Flet validates control
arguments in `__init__`, so simply constructing the tree catches the whole
class of "wrong keyword for this Flet version" bug. Two real ones were
found this way rather than in the browser:

  * `PoolDetailView` assigned `self.page`, but `ft.Column` already defines
    `page` as a read-only property;
  * `ft.Tab` took a `content=` argument in older Flet and does not in 0.86,
    where the bodies moved to `TabBarView`.

Both only surfaced on the *second* click in a published build. A constructor
test finds them in 0.2s.

Note this is not Flet's own integration testing -- see README for that. It
needs no Flutter SDK because it never renders anything; it only builds the
Python-side control tree.
"""

from __future__ import annotations

import flet as ft
import pytest

from curve.models import Pool
from ui.actions import DepositTab, StakeTab, SwapTab, WithdrawTab
from ui.candles import CandleChart
from ui.pool_detail import PoolDetailView
from ui.pool_list import PoolListView, PoolRow


class StubPage:
    """Stands in for `ft.Page`. Records instead of rendering."""

    def __init__(self) -> None:
        self.updates = 0
        self.tasks: list = []

    def update(self) -> None:
        self.updates += 1

    def run_task(self, handler, *args) -> None:
        self.tasks.append(handler)


def make_pool(n_coins: int = 2, *, registry: str = "factory-crvusd", gauge: str = "0xg") -> Pool:
    return Pool.from_api(
        {
            "address": "0x" + "1" * 40,
            "name": "Curve.fi Test",
            "symbol": "TEST",
            "registryId": registry,
            "gaugeAddress": gauge,
            "gaugeCrvApy": [2.93, 7.32],
            "gaugeRewards": [{"symbol": "OP", "apy": 1.2, "tokenAddress": "0x" + "2" * 40}],
            "usdTotal": 47_490_000.0,
            "coins": [
                {
                    "symbol": f"C{i}",
                    "address": "0x" + f"{i:02x}" * 20,
                    "decimals": "18",
                    "usdPrice": 1.0,
                    "poolBalance": str(10**21),
                }
                for i in range(n_coins)
            ],
        }
    )


# -- list ------------------------------------------------------------------


def test_pool_list_builds_and_sorts_without_a_page() -> None:
    view = PoolListView(on_open=lambda _p: None)
    pools = [make_pool(), make_pool(3)]
    view.set_pools(pools)
    assert len(view.rows.controls) == 2
    assert "2 pools" in view.count_label.value


def test_pool_row_builds_for_every_shape_of_rewards() -> None:
    for pool in (
        make_pool(),  # CRV + one incentive
        Pool.from_api({"address": "0x1", "coins": [{"symbol": "A"}]}),  # nothing
    ):
        assert PoolRow(pool, on_open=lambda _p: None) is not None


def test_switching_sort_rebuilds_the_rows() -> None:
    view = PoolListView(on_open=lambda _p: None)
    view.set_pools([make_pool(), make_pool(3)])
    for key in ("volume", "tvl", "incentives", "base"):
        view._sort_by(key)
        assert len(view.rows.controls) == 2


# -- detail ----------------------------------------------------------------


@pytest.mark.parametrize("n_coins", [2, 3, 4])
def test_pool_detail_builds_for_any_coin_count(n_coins: int) -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(n_coins), get_contract=lambda: None,
        on_back=lambda: None,
    )
    assert isinstance(view, ft.Column)
    # LP token, plus every ordered pair of coins
    assert len(view.series.options) == 1 + n_coins * (n_coins - 1)


def test_pool_detail_builds_without_a_gauge() -> None:
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(gauge=""), get_contract=lambda: None,
        on_back=lambda: None,
    )
    assert view is not None


def test_tabs_length_matches_the_number_of_panels() -> None:
    """`ft.Tabs.length` must match TabBar.tabs and TabBarView.controls."""
    view = PoolDetailView(
        StubPage(), api=None, pool=make_pool(), get_contract=lambda: None, on_back=lambda: None
    )
    assert len(view.tabs) == 4


# -- action tabs -----------------------------------------------------------


@pytest.mark.parametrize("tab_class", [DepositTab, WithdrawTab, SwapTab, StakeTab])
def test_action_tabs_mount_without_a_wallet(tab_class) -> None:
    async def noop() -> None:
        return None

    tab = tab_class(StubPage(), make_pool(3), lambda: None, noop)
    assert isinstance(tab.mount(), ft.Column)
    # Nothing is submittable until a wallet is connected.
    assert tab.submit_button.disabled


def test_stake_tab_says_so_when_there_is_no_gauge() -> None:
    async def noop() -> None:
        return None

    tab = StakeTab(StubPage(), make_pool(gauge=""), lambda: None, noop)
    tab.mount()
    text = " ".join(
        c.value for c in tab.control.controls if isinstance(c, ft.Text) and c.value
    )
    assert "no gauge" in text


def test_slippage_parsing_falls_back_on_nonsense() -> None:
    async def noop() -> None:
        return None

    tab = DepositTab(StubPage(), make_pool(), lambda: None, noop)
    tab.slippage.value = "1.5"
    assert tab.slippage_pct() == 1.5
    for bad in ("", "abc", "-1", "150"):
        tab.slippage.value = bad
        assert tab.slippage_pct() == 0.5


def test_deposit_parses_per_coin_decimals() -> None:
    async def noop() -> None:
        return None

    pool = make_pool(2)
    pool.coins[1].decimals = 6
    tab = DepositTab(StubPage(), pool, lambda: None, noop)
    tab.mount()
    tab.fields[0].value = "1"
    tab.fields[1].value = "1"
    assert tab._amounts() == [10**18, 10**6]


def test_deposit_ignores_unparseable_input_rather_than_raising() -> None:
    async def noop() -> None:
        return None

    tab = DepositTab(StubPage(), make_pool(2), lambda: None, noop)
    tab.mount()
    tab.fields[0].value = "not a number"
    assert tab._amounts() == [0, 0]


# -- chart -----------------------------------------------------------------


def test_candle_chart_builds_and_accepts_an_empty_series() -> None:
    chart = CandleChart()
    chart.set_candles([])
    assert chart._empty.visible
