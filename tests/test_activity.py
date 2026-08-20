"""Trades and liquidity: what the v1 endpoints say, and how a row reads it."""

from __future__ import annotations

import flet as ft
import pytest

from curve import api as api_module
from curve.api import ACTIVITY_ROWS, CurveApi, LiquidityEvent, Trade
from curve.http import ApiError
from curve.models import Pool
from ui.activity import (
    liquidity_row,
    moved_coins,
    trade_row,
)

DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
POOL = "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"


def token(symbol: str, address: str, index: int) -> dict:
    return {
        "symbol": symbol,
        "address": address,
        "pool_index": index,
        "event_index": index,
    }


def trade(sold_id: int, bought_id: int, when: str, tx: str = "0xaa") -> dict:
    return {
        "sold_id": sold_id,
        "bought_id": bought_id,
        "tokens_sold": 100.0,
        "tokens_bought": 99.99,
        "time": when,
        "transaction_hash": tx,
        "buyer": "0x1111111111111111111111111111111111111111",
    }


def _param(url: str, name: str) -> str:
    for part in url.partition("?")[2].split("&"):
        key, _, value = part.partition("=")
        if key == name:
            return value
    return ""


class Prices:
    """The v1 host, answering for one pair at a time."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.dead_pairs: set[str] = set()
        self.everything_down = False

    async def get_json(self, url: str, timeout: float = 30.0):
        self.asked.append(url)
        if self.everything_down:
            raise ApiError("prices is down")
        if "/liquidity/" in url:
            return {
                "data": [
                    {
                        "liquidity_event_type": "AddLiquidity",
                        "token_amounts": [0.0, 99673.826566, 0.0],
                        "time": "2026-08-20T11:36:35",
                        "transaction_hash": "0xadd",
                        "provider": "0x0c93D1A748bC6e6030fb628867d3b69Ce1d77f34",
                    },
                    {
                        "liquidity_event_type": "RemoveLiquidityOne",
                        "token_amounts": [0.0, 0.0, 99736.126773],
                        "time": "2026-08-20T11:30:00",
                        "transaction_hash": "0xout",
                        "provider": "0x0c93D1A748bC6e6030fb628867d3b69Ce1d77f34",
                    },
                ]
            }
        main, reference = _param(url, "main_token"), _param(url, "reference_token")
        if main in self.dead_pairs or reference in self.dead_pairs:
            raise ApiError(f"no trades for {main}/{reference}")
        names = {DAI: ("DAI", 0), USDC: ("USDC", 1), USDT: ("USDT", 2)}
        head, tail = names[main], names[reference]
        return {
            "main_token": token(head[0], main, head[1]),
            "reference_token": token(tail[0], reference, tail[1]),
            "data": [
                trade(tail[1], head[1], f"2026-08-20T11:{10 + head[1] + tail[1]}:00",
                      tx=f"0x{head[0]}{tail[0]}")
            ],
        }


@pytest.fixture
def prices(monkeypatch):
    served = Prices()
    monkeypatch.setattr(api_module, "get_json", served.get_json)
    return served


# -- reading the endpoints -------------------------------------------------


async def test_a_trade_names_the_coin_each_side_of_it(prices) -> None:
    """The rows carry pool indices, not symbols. Which index is which comes
    from the pair the answer was asked for.
    """
    trades = await CurveApi().trades("ethereum", POOL, [DAI, USDC])

    assert len(trades) == 1
    swap = trades[0]
    assert (swap.sold, swap.sold_amount) == ("USDC", 100.0)
    assert (swap.bought, swap.bought_amount) == ("DAI", 99.99)
    assert swap.sold_address == USDC
    assert swap.tx == "0xDAIUSDC"


async def test_every_pair_is_asked_for_once(prices) -> None:
    """The endpoint answers for one pair at a time, so a three-coin pool is
    three calls -- each unordered, because one answer holds both directions.
    """
    await CurveApi().trades("ethereum", POOL, [DAI, USDC, USDT])

    pairs = {
        (_param(url, "main_token"), _param(url, "reference_token"))
        for url in prices.asked
    }
    assert pairs == {(DAI, USDC), (DAI, USDT), (USDC, USDT)}


async def test_the_pairs_are_merged_newest_first(prices) -> None:
    trades = await CurveApi().trades("ethereum", POOL, [DAI, USDC, USDT])

    assert [t.time for t in trades] == sorted((t.time for t in trades), reverse=True)
    assert len(trades) == 3


async def test_one_dead_pair_does_not_empty_the_table(prices) -> None:
    prices.dead_pairs = {USDT}

    trades = await CurveApi().trades("ethereum", POOL, [DAI, USDC, USDT])

    assert [t.tx for t in trades] == ["0xDAIUSDC"]


async def test_every_pair_failing_is_an_error(prices) -> None:
    """Nothing to show and a reason for it: the table says the reason
    rather than "no swaps", which would be a different thing.
    """
    prices.everything_down = True

    with pytest.raises(ApiError):
        await CurveApi().trades("ethereum", POOL, [DAI, USDC])


async def test_a_pool_with_no_coins_asks_nothing(prices) -> None:
    assert await CurveApi().trades("ethereum", POOL, []) == []
    assert prices.asked == []


async def test_trades_are_cached_like_everything_else(prices) -> None:
    api = CurveApi()
    await api.trades("ethereum", POOL, [DAI, USDC])
    await api.trades("ethereum", POOL, [DAI, USDC])

    assert len(prices.asked) == 1


async def test_liquidity_reads_adds_and_takes_apart(prices) -> None:
    events = await CurveApi().liquidity("ethereum", POOL)

    assert [e.added for e in events] == [True, False]
    assert events[0].amounts == (0.0, 99673.826566, 0.0)
    assert events[0].provider.startswith("0x0c93")
    assert events[0].time > events[1].time


async def test_a_table_asks_for_as_many_rows_as_it_shows(prices) -> None:
    await CurveApi().liquidity("ethereum", POOL)

    assert _param(prices.asked[0], "per_page") == str(ACTIVITY_ROWS)


def test_a_bad_timestamp_is_zero_rather_than_a_crash() -> None:
    parsed = Trade.from_api(
        trade(0, 1, "not a time"), token("DAI", DAI, 0), token("USDC", USDC, 1)
    )
    assert parsed.time == 0


def test_an_index_in_neither_half_of_the_pair_falls_back() -> None:
    """A metapool can report an id the pair does not name. It draws the
    main token rather than raising in the middle of a table.
    """
    parsed = Trade.from_api(
        trade(7, 9, "2026-08-20T11:36:35"), token("DAI", DAI, 0), token("USDC", USDC, 1)
    )
    assert parsed.sold == "DAI" and parsed.bought == "DAI"


# -- how a row reads it ----------------------------------------------------


def make_pool() -> Pool:
    return Pool.from_v2(
        {
            "address": POOL,
            "chain_id": 1,
            "name": "DAI/USDC/USDT",
            "coins": [
                {"address": DAI, "symbol": "DAI", "decimals": 18},
                {"address": USDC, "symbol": "USDC", "decimals": 6},
                {"address": USDT, "symbol": "USDT", "decimals": 6},
            ],
        },
        chain="ethereum",
    )


def texts(control, found: list[str] | None = None) -> list[str]:
    """Every string drawn under a control."""
    found = [] if found is None else found
    if isinstance(control, ft.Text):
        found.append(control.value or "")
    for name in ("content", "controls", "leading", "title", "trailing"):
        child = getattr(control, name, None)
        if isinstance(child, list):
            for item in child:
                texts(item, found)
        elif child is not None:
            texts(child, found)
    return found


def test_a_swap_row_reads_left_to_right() -> None:
    row = trade_row(
        Trade(
            time=1787225879,
            tx="0xfeed",
            trader="0x1",
            sold="DAI",
            sold_address=DAI,
            sold_amount=100.0,
            bought="USDC",
            bought_address=USDC,
            bought_amount=99.99,
        ),
        "ethereum",
        1,
    )

    drawn = texts(row)
    assert "100 DAI" in drawn
    assert "99.99 USDC" in drawn


def test_a_swap_row_is_a_link_to_the_transaction() -> None:
    row = trade_row(
        Trade(1787225879, "0xfeed", "0x1", "DAI", DAI, 100.0, "USDC", USDC, 99.99),
        "ethereum",
        1,
    )

    assert isinstance(row.url, ft.Url)
    assert row.url.url == "https://etherscan.io/tx/0xfeed"


def test_a_row_without_a_hash_is_not_a_link() -> None:
    row = trade_row(
        Trade(1787225879, "", "0x1", "DAI", DAI, 100.0, "USDC", USDC, 99.99),
        "ethereum",
        1,
    )

    assert row.url is None


def test_only_the_coins_that_moved_are_drawn() -> None:
    """The API sends one amount per coin in the pool, zero for the rest --
    a one-sided withdrawal is two zeroes and a number.
    """
    event = LiquidityEvent(
        time=1787225795,
        tx="0xout",
        provider="0x0c93D1A748bC6e6030fb628867d3b69Ce1d77f34",
        added=False,
        amounts=(0.0, 0.0, 99736.126773),
    )

    assert [coin.symbol for coin, _ in moved_coins(event, make_pool())] == ["USDT"]
    drawn = texts(liquidity_row(event, make_pool()))
    assert any("99736" in text.replace(",", "") for text in drawn)
    assert "0x0c93…7f34" in drawn


def test_a_deposit_and_a_withdrawal_are_told_apart() -> None:
    put_in = LiquidityEvent(1, "0xa", "0xprovider", True, (1.0, 0.0, 0.0))
    took_out = LiquidityEvent(1, "0xb", "0xprovider", False, (1.0, 0.0, 0.0))

    assert "+" in texts(liquidity_row(put_in, make_pool()))
    assert "−" in texts(liquidity_row(took_out, make_pool()))  # noqa: RUF001


def test_a_liquidity_row_links_to_its_transaction() -> None:
    event = LiquidityEvent(1, "0xdeed", "0xprovider", True, (1.0, 0.0, 0.0))

    row = liquidity_row(event, make_pool())

    assert row.url.url == "https://etherscan.io/tx/0xdeed"
