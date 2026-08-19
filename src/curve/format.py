"""Turning numbers into the strings a dense table can show."""

from __future__ import annotations

from decimal import Decimal


def compact_usd(value: float, *, sign: str = "$") -> str:
    """`$159.98m`, `$206.90k`, `$47.49` -- two decimals, lowercase suffix."""
    if value is None:
        return "-"
    magnitude = abs(value)
    if magnitude >= 1e12:
        return f"{sign}{value / 1e12:.2f}t"
    if magnitude >= 1e9:
        return f"{sign}{value / 1e9:.2f}b"
    if magnitude >= 1e6:
        return f"{sign}{value / 1e6:.2f}m"
    if magnitude >= 1e3:
        return f"{sign}{value / 1e3:.2f}k"
    if magnitude == 0:
        return f"{sign}0"
    return f"{sign}{value:.2f}"


def percent(value: float, *, places: int = 2) -> str:
    """`1.27%`, and `< 0.01%` for anything that would round to zero."""
    if value is None:
        return "-"
    if value == 0:
        return "0%"
    smallest = 10.0**-places
    if 0 < abs(value) < smallest:
        return f"< {smallest:.{places}f}%" if value > 0 else f"> -{smallest:.{places}f}%"
    return f"{value:.{places}f}%"


def apr_range(low: float, high: float) -> str:
    """`2.93% to 7.32%` for a boost range, collapsing when the ends match."""
    if not low and not high:
        return "-"
    if abs(high - low) < 1e-9:
        return percent(high)
    return f"{percent(low)} to {percent(high)}"


def token_amount(value: float, *, places: int = 4) -> str:
    """A human token quantity: grouped, trailing zeros trimmed."""
    if value is None:
        return "-"
    if value == 0:
        return "0"
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def units_to_float(value: int, decimals: int) -> float:
    """Smallest units -> float, for display only."""
    if decimals <= 0:
        return float(value)
    return float(Decimal(value) / (Decimal(10) ** decimals))


def short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def price(value: float) -> str:
    """A coin's USD price, with enough precision to be useful."""
    if not value:
        return "-"
    if abs(value) >= 1000:
        return f"${value:,.2f}"
    text = f"{value:,.5f}".rstrip("0").rstrip(".")
    return f"${text}"
