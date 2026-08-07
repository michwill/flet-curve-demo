"""Reading a chain with no wallet attached.

Every quote in this app is read from the pool itself, and until now that
read went through the connected wallet's provider -- so with no wallet
there was no rate to show, on a screen whose whole point is showing rates.
Nothing about a `get_dy` needs an account.

So this is the other kind of provider: a plain JSON-RPC client over public
endpoints, implementing enough of `WalletProvider` for `PoolContract` to
work unchanged. It reads and it refuses to sign, which is the honest
division -- there is no key here to sign with.

**The endpoints come from chainlist.org**, which publishes what the
community keeps working (`rpcs.json`, CORS-open, so the browser build can
read it too). Two things about that list shape the code:

  * it is a list *because they fall over*. Rate limits, dead hosts, a
    endpoint that answers HTML from a captive portal. So a request walks
    the list until one answers, and the survivor is remembered so the next
    read starts where the last one succeeded rather than at the top;
  * some entries are not usable from here at all -- API-key templates
    (`${INFURA_API_KEY}`), websockets, and in a browser anything without
    CORS. The first two are filtered by inspection; the third cannot be
    known in advance and is simply another failure to walk past.

A wallet, when there is one, is still preferred: it is the user's own node
or their provider's, it is what will actually execute the transaction, and
a quote read from the same place the transaction will run is the quote
least likely to be a surprise.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wallet.base import RpcError, WalletError, WalletProvider

from .http import ApiError, get_json, post_json

#: Chainlist's own published list. `chainid.network/chains.json` is the
#: same data upstream, but this one carries the `tracking` field, which is
#: the only privacy signal available for choosing between strangers'
#: endpoints.
CHAINLIST = "https://chainlist.org/rpcs.json"

#: How long to wait on one endpoint before trying the next. Short: the
#: point of a list is that giving up is cheap, and a quote that takes
#: thirty seconds is a quote nobody waited for.
ENDPOINT_TIMEOUT = 8.0

#: How many of a chain's endpoints to keep. The full list runs to eighty
#: for Ethereum, and walking eighty dead hosts is not failover, it is a
#: hang.
MAX_ENDPOINTS = 8

#: Preferred first: endpoints that say they keep nothing. `tracking` is
#: self-reported, so this is a nudge rather than a guarantee.
_TRACKING_RANK = {"none": 0, "limited": 1, "yes": 3}


def usable_endpoints(chain: dict[str, Any], limit: int = MAX_ENDPOINTS) -> list[str]:
    """The endpoints from one chainlist entry this app can actually call.

    Dropped: websockets (this speaks HTTP), anything with an API-key
    template in it, and plain `http://`, which a browser on an HTTPS page
    will not load anyway.
    """
    scored: list[tuple[int, str]] = []
    for entry in chain.get("rpc") or []:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        if "${" in url:
            continue
        tracking = entry.get("tracking") if isinstance(entry, dict) else None
        scored.append((_TRACKING_RANK.get(tracking or "", 2), url))
    # Stable within a rank, so the order chainlist publishes is preserved
    # among equals -- it roughly tracks how well they work.
    scored.sort(key=lambda pair: pair[0])
    return [url for _rank, url in scored[:limit]]


class ChainlistDirectory:
    """chainlist.org, fetched once and kept.

    One request for every chain rather than one per chain: there is no
    per-chain endpoint, and the file is a couple of megabytes. It is
    fetched the first time a quote needs a node and not before, so a
    session with a wallet connected never asks for it at all.
    """

    def __init__(self, url: str = CHAINLIST) -> None:
        self.url = url
        self._by_chain: dict[int, list[str]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def endpoints(self, chain_id: int) -> list[str]:
        """Public endpoints for a chain, or an empty list if unknown."""
        async with self._lock:
            if not self._loaded:
                await self._load()
        return list(self._by_chain.get(chain_id, ()))

    async def _load(self) -> None:
        # Marked loaded either way: a directory that will not answer is
        # not worth re-fetching on every keystroke in an amount field.
        self._loaded = True
        try:
            payload = await get_json(self.url, timeout=20.0)
        except ApiError:
            return
        if not isinstance(payload, list):
            return
        for chain in payload:
            if not isinstance(chain, dict) or chain.get("isTestnet"):
                continue
            chain_id = chain.get("chainId")
            if chain_id is None:
                continue
            endpoints = usable_endpoints(chain)
            if endpoints:
                self._by_chain[int(chain_id)] = endpoints


class PublicNode(WalletProvider):
    """A read-only provider over several public endpoints.

    Implements the reading half of `WalletProvider`, so `PoolContract`
    cannot tell the difference; the signing half raises, because there is
    no account here to sign with.
    """

    def __init__(
        self,
        network_id: int,
        directory: ChainlistDirectory,
        *,
        timeout: float = ENDPOINT_TIMEOUT,
    ) -> None:
        #: Not `chain_id`: that name is the provider's *method*, and an
        #: attribute of the same name would shadow it.
        self.network_id = network_id
        self.directory = directory
        self.timeout = timeout
        self._endpoints: list[str] = []
        self._next = 0
        self._counter = 0

    async def _ensure_endpoints(self) -> list[str]:
        if not self._endpoints:
            self._endpoints = await self.directory.endpoints(self.network_id)
        return self._endpoints

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        """Walk the endpoints until one answers.

        A JSON-RPC *error* is the endpoint doing its job -- a reverted
        `eth_call` is an answer, and asking somebody else the same
        question gets the same answer -- so it is raised rather than
        retried. Only a failure to be answered at all moves on.
        """
        endpoints = await self._ensure_endpoints()
        if not endpoints:
            raise WalletError(
                "No public node is known for this network, so nothing can be "
                "read without a wallet."
            )
        self._counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._counter,
            "method": method,
            "params": params or [],
        }
        last = ""
        for offset in range(len(endpoints)):
            index = (self._next + offset) % len(endpoints)
            url = endpoints[index]
            try:
                answer = await post_json(url, payload, timeout=self.timeout)
            except ApiError as exc:
                last = str(exc)
                continue
            if not isinstance(answer, dict):
                last = f"{url} answered something that was not JSON-RPC"
                continue
            if "error" in answer:
                # Remember this one: it is working, it just said no.
                self._next = index
                error = answer["error"] or {}
                raise RpcError(
                    int(error.get("code", -1) or -1), str(error.get("message", ""))
                )
            self._next = index
            return answer.get("result")
        raise WalletError(f"No public node answered for this network. {last}".strip())

    async def send_transaction(self, tx: dict[str, Any]) -> str:
        raise WalletError("Connect a wallet to send a transaction.")

    async def sign_message(self, address: str, message: str) -> str:
        raise WalletError("Connect a wallet to sign a message.")

    async def switch_chain(self, chain_id: int) -> None:
        raise WalletError("Connect a wallet to switch network.")

    async def chain_id(self) -> int:
        """What this node is for. Known without asking -- and asking a
        stranger's endpoint would only be a chance to disagree."""
        return self.network_id
