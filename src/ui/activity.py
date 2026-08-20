"""What went through a pool, row by row: swaps, and liquidity moved."""

from __future__ import annotations

import flet as ft

from curve import explorers
from curve.api import LiquidityEvent, Trade
from curve.format import date_time, short_address, token_amount
from curve.models import Coin, Pool

from .logos import token_mark
from .typography import LABEL, SMALL

#: A coin mark beside its amount.
MARK = 18

#: Deposits and withdrawals, told apart at a glance.
ADDED = ft.Colors.GREEN_600
TAKEN = ft.Colors.ERROR

ROW_PADDING = ft.Padding.symmetric(vertical=5, horizontal=6)

#: What an empty table says, per kind.
NO_TRADES = "No swaps through this pool yet."
NO_LIQUIDITY = "Nothing has been added or taken out yet."


def _linked(content: ft.Control, url: str) -> ft.Control:
    """A row, and the transaction behind it."""
    return ft.Container(
        content,
        padding=ROW_PADDING,
        border_radius=4,
        ink=bool(url),
        url=ft.Url(url, target=ft.UrlTarget.BLANK) if url else None,
        tooltip="Open the transaction" if url else None,
    )


def _amount(symbol: str, address: str, amount: float, chain: str) -> ft.Control:
    """A mark and what there was of it."""
    coin = Coin(address=address, symbol=symbol, decimals=18)
    return ft.Row(
        [
            token_mark(coin, chain, MARK),
            ft.Text(f"{token_amount(amount)} {symbol}", size=SMALL, no_wrap=True),
        ],
        spacing=5,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def _stamp(time: int) -> ft.Control:
    return ft.Text(
        date_time(time), size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True
    )


def trade_row(
    trade: Trade, chain: str, chain_id: int, explorer: str = ""
) -> ft.Control:
    """`100 DAI -> 99.99 USDC`, and when."""
    return _linked(
        ft.Row(
            [
                _amount(trade.sold, trade.sold_address, trade.sold_amount, chain),
                ft.Icon(
                    ft.Icons.ARROW_FORWARD,
                    size=14,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
                _amount(trade.bought, trade.bought_address, trade.bought_amount, chain),
                ft.Container(expand=True),
                _stamp(trade.time),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        explorers.tx_url(chain_id, trade.tx, explorer),
    )


def moved_coins(event: LiquidityEvent, pool: Pool) -> list[tuple[Coin, float]]:
    """The coins this event actually moved. The API sends one amount per
    coin in the pool and zero for the rest, including for a one-sided
    withdrawal.
    """
    return [
        (coin, amount)
        for coin, amount in zip(pool.pool_coins, event.amounts, strict=False)
        if amount
    ]


def liquidity_row(
    event: LiquidityEvent, pool: Pool, explorer: str = ""
) -> ft.Control:
    """What was put in or taken out, by whom, and when."""
    moved = moved_coins(event, pool)
    sign = "+" if event.added else "−"  # noqa: RUF001 -- a minus, not a hyphen
    amounts: list[ft.Control] = [
        ft.Text(sign, size=SMALL, weight=ft.FontWeight.W_600,
                color=ADDED if event.added else TAKEN),
    ]
    if moved:
        amounts += [
            _amount(coin.symbol, coin.address, amount, pool.chain)
            for coin, amount in moved
        ]
    else:
        amounts.append(ft.Text("–", size=SMALL, color=ft.Colors.OUTLINE))  # noqa: RUF001
    return _linked(
        ft.Row(
            [
                ft.Row(amounts, spacing=8, wrap=True, expand=True,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Text(
                    short_address(event.provider),
                    size=LABEL,
                    font_family="monospace",
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    no_wrap=True,
                ),
                _stamp(event.time),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        explorers.tx_url(pool.chain_id, event.tx, explorer),
    )


def empty(message: str) -> ft.Control:
    return ft.Container(
        ft.Text(message, size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
        alignment=ft.Alignment.CENTER,
        padding=ft.Padding.symmetric(vertical=24),
    )
