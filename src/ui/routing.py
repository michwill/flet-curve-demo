"""What the address bar says, and what it means."""

from __future__ import annotations

from dataclasses import dataclass

#: The one second segment that is a page rather than a pool.
PORTFOLIO = "portfolio"


@dataclass(frozen=True, slots=True)
class Route:
    """A parsed route. Empty strings mean "not specified"."""

    chain: str = ""
    pool: str = ""
    page: str = ""

    @property
    def is_pool(self) -> bool:
        return bool(self.chain and self.pool)

    @property
    def is_portfolio(self) -> bool:
        return self.page == PORTFOLIO


def parse(route: str | None) -> Route:
    """Read a route. Anything unrecognisable comes back empty."""
    parts = [part for part in (route or "").split("/") if part]
    if not parts:
        return Route()
    chain = parts[0].lower()
    if len(parts) == 1:
        return Route(chain)
    second = parts[1]
    if second.lower() == PORTFOLIO:
        return Route(chain, page=PORTFOLIO)
    if not _looks_like_address(second):
        return Route(chain)
    return Route(chain, second)


def build(chain: str = "", pool: str = "", page: str = "") -> str:
    """The route for a chain, and optionally a pool or a page on it."""
    if not chain:
        return "/"
    if page:
        return f"/{chain.lower()}/{page}"
    if not pool:
        return f"/{chain.lower()}"
    return f"/{chain.lower()}/{pool}"


def same_pool(left: str, right: str) -> bool:
    """Addresses compare case-insensitively: a checksummed address and a
    lowercased one are the same pool, and both turn up in URLs.
    """
    return bool(left) and left.lower() == right.lower()


def _looks_like_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])
