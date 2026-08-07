"""Curve Lite: the small deployments, on a different API.

Most chains in this app come from the Prices API, which indexes trades and
so can report volume, base APR and price history. **Curve Lite** is the
other kind of deployment -- the factory contracts and a gauge, without any
of that indexing -- and it is served by `api2.curve.finance`, a separate
service with six endpoints and a different shape.

What is missing is the point, and it is not a little:

  * no volume and no base APR. Not zero -- *unknown*. Nothing measures
    trades on these chains, so a list that printed 0.00% would be
    inventing a measurement;
  * no OHLC, so no chart;
  * no CRV boost range, and reward APRs only where the token has a price.

What it does give, in one request per chain, is everything the pools
themselves know: reserves, coin prices, LP token, gauge, amplification.
That is the whole chain at once rather than a page at a time, so ordering,
searching and paging happen here instead of at the server -- `select`
below is the local stand-in for what v2 does with query parameters.

Two lists are filtered out before anything is shown: pools the API marks
`is_broken`, and the ones `get_hidden_pools` names -- Curve's own frontend
hides both, and a pool that cannot be read is worse than a pool that is
absent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import Pool

LITE_API = "https://api2.curve.finance"

#: The list floor for a Lite chain. Zero, where the main chains use
#: $10,000: whole Lite deployments are smaller than that floor -- Sonic's
#: pools come to about $200k between them -- so the same cut would empty
#: the list. Sorting by TVL is what keeps the dust at the bottom.
LITE_MIN_TVL = 0.0


@dataclass(frozen=True, slots=True)
class LiteChain:
    """One Curve Lite deployment, from `get_platforms`."""

    name: str
    chain_id: int
    #: The display name the API carries, which is sometimes better cased
    #: than the key ("X layer" for `x-layer`) and sometimes worse ("tac").
    label: str
    tvl: float
    #: What a wallet needs to *add* the network, for the ones no wallet
    #: ships with -- which is most of these. `wallet_addEthereumChain`
    #: wants exactly this: a name, an RPC, an explorer and the native
    #: symbol. Nowhere else publishes it for a Curve Lite chain.
    rpc_url: str = ""
    explorer: str = ""
    native_symbol: str = "ETH"


def parse_platforms(payload: Any) -> dict[str, LiteChain]:
    """Chain name -> deployment, mainnets only.

    The testnets and devnets in this list (`bsc_testnet`, `devnet:monad`)
    are real entries with real pools, and no use at all to someone opening
    a pool list, so they are dropped here rather than shown and explained.
    """
    data = (payload or {}).get("data") or {}
    metadata = data.get("platforms_metadata") or {}
    chains: dict[str, LiteChain] = {}
    for name, meta in metadata.items():
        if not isinstance(meta, dict) or not meta.get("is_mainnet"):
            continue
        chain_id = meta.get("chain_id")
        if chain_id is None:
            continue
        chains[name] = LiteChain(
            name=name,
            chain_id=int(chain_id),
            label=str(meta.get("name") or name),
            tvl=float(meta.get("tvl") or 0.0),
            rpc_url=str(meta.get("rpc_url") or ""),
            explorer=str(meta.get("explorer_base_url") or ""),
            native_symbol=str(meta.get("native_currency_symbol") or "ETH"),
        )
    return chains


def parse_hidden(payload: Any) -> set[tuple[int, str]]:
    """`(chain_id, address)` pairs Curve's own frontend does not show."""
    hidden: set[tuple[int, str]] = set()
    for entry in (payload or {}).get("data") or []:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        chain_id = entry.get("chain_id")
        if address and chain_id is not None:
            hidden.add((int(chain_id), str(address).lower()))
    return hidden


def parse_pools(
    payload: Any, chain: str, hidden: set[tuple[int, str]] | None = None
) -> list[Pool]:
    """Every pool on one Lite chain, minus the ones nobody should see."""
    data = (payload or {}).get("data") or {}
    hidden = hidden or set()
    pools = []
    for raw in data.get("pool_data") or []:
        if not isinstance(raw, dict) or raw.get("is_broken"):
            continue
        pool = Pool.from_lite(raw, chain)
        if (pool.chain_id, pool.address.lower()) in hidden:
            continue
        pools.append(pool)
    return pools


def matches(pool: Pool, query: str) -> bool:
    """The local stand-in for v2's `search_string`.

    Name, coin symbols and address, because those are the three things
    someone has in hand: what the pool is called, what is in it, or a
    contract they pasted.
    """
    query = query.strip().lower()
    if not query:
        return True
    if query in pool.name.lower() or query in pool.address.lower():
        return True
    return any(query in coin.symbol.lower() for coin in pool.coins)


def select(
    pools: Iterable[Pool],
    *,
    sort_local,
    direction: str = "desc",
    search: str = "",
    min_tvl: float | None = LITE_MIN_TVL,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Pool], int]:
    """Filter, order and page a whole chain in memory.

    Returns the requested page and the total matching count, which is the
    same contract `CurveApi.list_pools` has with v2 -- so `PoolFeed` pages
    a Lite chain exactly as it pages a big one, and nothing above this
    knows the difference.
    """
    found = [
        pool
        for pool in pools
        if (not min_tvl or pool.tvl >= min_tvl) and matches(pool, search)
    ]
    found.sort(key=sort_local, reverse=direction != "asc")
    start = max(0, (max(1, page) - 1) * page_size)
    return found[start : start + page_size], len(found)
