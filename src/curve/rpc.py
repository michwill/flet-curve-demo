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

Preferred, but no longer trusted to be the only one asked. A wallet is a
transport as much as a node -- over WalletConnect it is a relay and a
phone -- and it can fail to carry a read that any public endpoint would
have answered. `FallbackProvider` puts this list behind it, so a wallet
that will not answer costs a retry rather than the page.
"""

from __future__ import annotations

import asyncio
import time
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

#: How long a failed directory fetch is allowed to stand before it is
#: tried again.
#:
#: Both halves matter. A quote is typed a character at a time, so
#: re-fetching two megabytes per keystroke would be worse than having no
#: quote -- that is why there is a wait at all. But the wait used to be
#: *forever*: one failed fetch left the session with no endpoints for any
#: chain until the page was reloaded, and since every read then failed,
#: the pool panel reported it as a pool with no parameters. A directory
#: this app cannot do without should not be given up on after one try.
RETRY_AFTER = 30.0


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
        #: When the last attempt failed, on the monotonic clock. `None`
        #: means no attempt has failed since the last success.
        self._failed_at: float | None = None
        self._lock = asyncio.Lock()

    async def endpoints(self, chain_id: int) -> list[str]:
        """Public endpoints for a chain, or an empty list if unknown."""
        async with self._lock:
            if not self._loaded and self._due():
                await self._load()
        return list(self._by_chain.get(chain_id, ()))

    def _due(self) -> bool:
        """Whether a fetch is worth making now. See `RETRY_AFTER`."""
        if self._failed_at is None:
            return True
        return time.monotonic() - self._failed_at >= RETRY_AFTER

    async def _load(self) -> None:
        """Fetch the directory, and record whether it worked.

        Held to the one thing the caller needs: a chain that answers with
        endpoints. Anything short of that -- the request failing, or a
        captive portal answering something that is not the list -- leaves
        it to be tried again rather than settled.
        """
        try:
            payload = await get_json(self.url, timeout=20.0)
        except ApiError:
            payload = None
        for chain in payload if isinstance(payload, list) else ():
            if not isinstance(chain, dict) or chain.get("isTestnet"):
                continue
            chain_id = chain.get("chainId")
            if chain_id is None:
                continue
            endpoints = usable_endpoints(chain)
            if endpoints:
                self._by_chain[int(chain_id)] = endpoints
        if self._by_chain:
            self._loaded = True
            self._failed_at = None
            return
        self._failed_at = time.monotonic()


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


#: Methods that only ever read *the chain*. Everything else -- a
#: signature, a send, a chain switch -- belongs to the user's own wallet
#: and to nothing else, however badly that wallet is behaving. There is no
#: "fallback" for a signature, and a public node could not provide one if
#: there were.
#:
#: `eth_chainId` is deliberately absent, and it is the one that would have
#: done damage. It does not read the chain; it asks a *source* which chain
#: it is on, and every source here has a different honest answer. The
#: action panel asks it to decide whether the wallet needs switching
#: networks before it can act (`ui.actions.network_ok`) -- answered by a
#: public node, that check would pass every time, and the "switch your
#: wallet to Ethereum" prompt would never appear again.
READ_METHODS = frozenset(
    {
        "eth_blockNumber",
        "eth_call",
        "eth_estimateGas",
        "eth_gasPrice",
        "eth_getBalance",
        "eth_getBlockByNumber",
        "eth_getCode",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
    }
)

#: Connectors that do not have a node of their own: the wallet is on
#: another device and every read is a round trip through a relay to reach
#: it. See `prefers_public_reads`.
RELAYED_CONNECTORS = frozenset({"walletconnect"})


def prefers_public_reads(provider: WalletProvider) -> bool:
    """Should the chain be read past this wallet rather than through it?

    Yes for WalletConnect, and only for WalletConnect. An injected wallet
    talks to a node over HTTP from the same browser; WalletConnect talks
    to a phone, over a relay built to carry signing prompts. A portfolio
    scan is not a signing prompt -- it is six Multicall3 batches of three
    hundred entries -- and pushing one through that link is how the
    portfolio came back `Load failed` instead of a list of positions.

    Reading past it is also *more* correct, not merely more reliable: the
    public node is pinned to the chain the app is showing, while the
    wallet answers for whatever chain it happens to be on.
    """
    return getattr(provider, "connector", "") in RELAYED_CONNECTORS


class FallbackProvider(WalletProvider):
    """Reads that survive their source falling over.

    A connected wallet is asked first, because it is the user's own view
    of the chain and the place their transaction will actually run. But
    "the user's wallet" is not always a node: over WalletConnect a read
    goes out to a relay and into a phone, and a portfolio scan is a
    Multicall3 batch of three hundred entries -- tens of kilobytes, six
    batches at a time. That is a lot to ask of a link built for passing
    signing requests, and when it will not carry it the answer came back
    `Load failed` and the whole page said it could not read the chain.

    So each read walks a list: the wallet, then the public endpoints from
    `PublicNode`, which walks its own list in turn. A source that cannot
    answer is stepped over.

    With `spares_first`, that order is reversed for reads and the wallet
    becomes the last resort rather than the first choice -- which is what
    a relayed connector wants (see `prefers_public_reads`). It stays in
    the list either way: on a chain chainlist has never heard of, the
    wallet is the only thing that can answer at all.

    With `read_primary=False` it is not in the list at all, and that is a
    different case from either: a wallet on *another chain* is not a slow
    source or an unreliable one, it is a source answering a question about
    Ethereum with what Fraxtal says. It answers successfully, so nothing
    above ever falls back -- `balanceOf` on a contract that does not exist
    there returns `0x`, which decodes to nothing held. A portfolio reads
    empty and no error is raised anywhere. See `CurveApp.reader`.

    Whichever way the reads go, *only* the primary is ever asked to sign,
    to send, or to switch networks. That is not a preference, it is the
    whole distinction: a public node has no key. Which is why the wallet
    is dropped from `sources` rather than from the object: the thing that
    must not be *read* through on the wrong chain is exactly the thing
    that must still be asked to move to the right one.

    What is *not* retried is a node that answered. An `RpcError` is a
    reply -- a reverted call, a rejected request -- and asking a different
    endpoint the same question gets the same reply, more slowly. Only a
    failure to be answered at all moves on. Same rule as `PublicNode`,
    for the same reason.
    """

    def __init__(
        self,
        primary: WalletProvider,
        *spares: WalletProvider,
        spares_first: bool = False,
        read_primary: bool = True,
    ) -> None:
        #: The wallet. Signs, sends, switches -- and names the transport
        #: in the UI, whatever order the reads go in.
        self.primary = primary
        self.spares: list[WalletProvider] = list(spares)
        #: Read order.
        self.sources: list[WalletProvider] = (
            [*spares, primary] if spares_first else [primary, *spares]
        )
        if not read_primary:
            self.sources = list(spares)
        self.name = getattr(primary, "name", "wallet")
        self.kind = getattr(primary, "kind", "unknown")
        self.connector = getattr(primary, "connector", "")

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in READ_METHODS:
            return await self.primary.request(method, params)
        last: WalletError | None = None
        for source in self.sources:
            try:
                return await source.request(method, params)
            except RpcError:
                # An answer, not a failure. See the class docstring.
                raise
            except WalletError as exc:
                last = exc
        raise last or WalletError("Nothing could be asked about this network.")

    async def close(self) -> None:
        """Only the spares. The wallet's session is not this object's to end."""
        for spare in self.spares:
            await spare.close()
