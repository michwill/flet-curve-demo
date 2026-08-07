"""What the address bar says, and what it means.

Flet gives a web build the browser's URL as `page.route`, pushes history
entries with `page.go`, and calls `on_route_change` when either the app or
the *user* navigates -- the Back button included. So a pool page can have
an address worth sending to somebody, and Back can mean what it looks
like it means.

Four shapes, and nothing else:

    /                       the list, on whatever chain is the default
    /ethereum               the list, on that chain
    /ethereum/0xC09e82…     that pool, on that chain
    /ethereum/portfolio     what this address holds, on that chain

`portfolio` is a reserved second segment. It cannot collide with a pool:
the second segment is otherwise required to look like an address, and
"portfolio" does not.

Chain names are the API's own (`xdai` for Gnosis, `x-layer`), because they
are what every other part of this app keys by; translating for the address
bar would mean maintaining a second set of names that could disagree.

This module is deliberately pure: parsing and building strings, no page
and no network, so the awkward parts -- junk paths, trailing slashes, an
address in the wrong case -- are testable without a browser.
"""

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
    """Read a route. Anything unrecognisable comes back empty.

    An empty result is not an error: it means the app should show what it
    would have shown anyway, which is the right response to a URL somebody
    typed by hand or a link that has rotted.
    """
    parts = [part for part in (route or "").split("/") if part]
    if not parts:
        return Route()
    chain = parts[0].lower()
    if len(parts) == 1:
        return Route(chain)
    second = parts[1]
    if second.lower() == PORTFOLIO:
        return Route(chain, page=PORTFOLIO)
    # A pool address, or nothing. Checking the shape here keeps every
    # caller from having to: `/ethereum/deposit` is not a pool page.
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
    lowercased one are the same pool, and both turn up in URLs."""
    return bool(left) and left.lower() == right.lower()


def _looks_like_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])
