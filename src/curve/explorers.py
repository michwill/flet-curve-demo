"""Where to send someone who wants to see a contract for themselves.

An address on screen is only half an answer: the other half is a link to
the chain's explorer. There is no API that hands out that link for every
chain this app lists, so it comes from two places.

**The Lite chains publish their own.** `get_platforms` carries
`explorer_base_url` for each of them, which is where the exact values for
Monad, Plume, Robinhood and the rest live -- see `LiteChain.explorer`.
Anything from there wins, because it is Curve's own answer and it stays
current without this file being edited.

**The main chains are the table below**, because their explorer is not in
any Curve endpoint. Ten chains, checked by opening one address on each.

Anything unknown falls back to blockscan.com, which searches an address
across chains and lands the user somewhere useful rather than nowhere.
That is deliberately not an error case: a chain this app has never heard
of should still show a clickable address.
"""

from __future__ import annotations

#: The chains Curve's v2 API covers, by id. Names are the API's.
EXPLORERS: dict[int, str] = {
    1: "https://etherscan.io",              # ethereum
    10: "https://optimistic.etherscan.io",  # optimism
    56: "https://bscscan.com",              # bsc
    100: "https://gnosisscan.io",           # xdai
    137: "https://polygonscan.com",         # polygon
    146: "https://sonicscan.org",           # sonic
    252: "https://fraxscan.com",            # fraxtal
    999: "https://hyperevmscan.io",         # hyperliquid
    8453: "https://basescan.org",           # base
    42161: "https://arbiscan.io",           # arbitrum
}

#: Multi-chain search. Not a real explorer for any one chain, but it
#: finds an address on most of them, which beats a dead link.
FALLBACK = "https://blockscan.com"


def base_url(chain_id: int, published: str = "") -> str:
    """The explorer for a chain.

    `published` is whatever the chain itself said -- a Lite chain's
    `explorer_base_url` -- and takes precedence over the table.
    """
    if published:
        return published.rstrip("/")
    return EXPLORERS.get(chain_id, FALLBACK)


def address_url(chain_id: int, address: str, published: str = "") -> str:
    """A link to one contract. Empty for an empty address."""
    if not address:
        return ""
    return f"{base_url(chain_id, published)}/address/{address}"
