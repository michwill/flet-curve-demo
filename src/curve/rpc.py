"""Reading a chain with no wallet attached."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from wallet.base import RpcError, WalletError, WalletProvider

from .http import ApiError, get_json, post_json

#: Chainlist's own published list. `chainid.network/chains.json` is the same
#: data upstream, but this one carries the `tracking` field, which is the
#: only privacy signal available for choosing between strangers' endpoints.
CHAINLIST = "https://chainlist.org/rpcs.json"

#: How long to wait on one endpoint before trying the next.
ENDPOINT_TIMEOUT = 8.0

#: How many of a chain's endpoints to keep.
MAX_ENDPOINTS = 8

#: Preferred first: endpoints that say they keep nothing.
_TRACKING_RANK = {"none": 0, "limited": 1, "yes": 3}

#: How long one source gets to answer a *read* before the next is asked.
READ_DEADLINE = 8.0

#: How long a source that could not answer is left out of the read order.
SOURCE_COOLDOWN = 30.0

#: How long a failed directory fetch is allowed to stand before it is tried
#: again.
RETRY_AFTER = 30.0


def usable_endpoints(chain: dict[str, Any], limit: int = MAX_ENDPOINTS) -> list[str]:
    """The endpoints from one chainlist entry this app can actually call."""
    scored: list[tuple[int, str]] = []
    for entry in chain.get("rpc") or []:
        url = entry.get("url") if isinstance(entry, dict) else entry
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        if "${" in url:
            continue
        tracking = entry.get("tracking") if isinstance(entry, dict) else None
        scored.append((_TRACKING_RANK.get(tracking or "", 2), url))
    scored.sort(key=lambda pair: pair[0])
    return [url for _rank, url in scored[:limit]]


class ChainlistDirectory:
    """chainlist.org, fetched once and kept."""

    def __init__(self, url: str = CHAINLIST) -> None:
        self.url = url
        self._by_chain: dict[int, list[str]] = {}
        self._loaded = False
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
        """Fetch the directory, and record whether it worked."""
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
    """A read-only provider over several public endpoints."""

    def __init__(
        self,
        network_id: int,
        directory: ChainlistDirectory,
        *,
        timeout: float = ENDPOINT_TIMEOUT,
    ) -> None:
        #: Not `chain_id`: that name is the provider's *method*, and
        #: an attribute of the same name would shadow it.
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
        """Walk the endpoints until one answers."""
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
        stranger's endpoint would only be a chance to disagree.
        """
        return self.network_id


#: Methods that only ever read *the chain*.
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

#: Connectors that do not have a node of their own: the wallet is on another
#: device and every read is a round trip through a relay to reach it.
RELAYED_CONNECTORS = frozenset({"walletconnect"})


def prefers_public_reads(provider: WalletProvider) -> bool:
    """Should the chain be read past this wallet rather than through it?"""
    return getattr(provider, "connector", "") in RELAYED_CONNECTORS


class FallbackProvider(WalletProvider):
    """Reads that survive their source falling over."""

    def __init__(
        self,
        primary: WalletProvider,
        *spares: WalletProvider,
        spares_first: bool = False,
        read_primary: bool = True,
    ) -> None:
        #: The wallet. Signs, sends, switches -- and names the
        #: transport in the UI, whatever order the reads go in.
        self.primary = primary
        self.spares: list[WalletProvider] = list(spares)
        self.sources: list[WalletProvider] = (
            [*spares, primary] if spares_first else [primary, *spares]
        )
        if not read_primary:
            self.sources = list(spares)
        self.name = getattr(primary, "name", "wallet")
        self.kind = getattr(primary, "kind", "unknown")
        self.connector = getattr(primary, "connector", "")
        self._cold: dict[int, float] = {}

    def read_order(self) -> list[int]:
        """The sources worth asking now, as positions in `self.sources`."""
        now = time.monotonic()
        warm = [
            index
            for index in range(len(self.sources))
            if now - self._cold.get(index, -SOURCE_COOLDOWN) >= SOURCE_COOLDOWN
        ]
        return warm or list(range(len(self.sources)))

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in READ_METHODS:
            return await self.primary.request(method, params)
        order = self.read_order()
        last: WalletError | None = None
        for position, index in enumerate(order):
            source = self.sources[index]
            deadline = READ_DEADLINE if position < len(order) - 1 else None
            try:
                answer = await self._ask(source, method, params, deadline)
            except RpcError:
                raise
            except WalletError as exc:
                last = exc
                self._cold[index] = time.monotonic()
                continue
            self._cold.pop(index, None)
            return answer
        raise last or WalletError("Nothing could be asked about this network.")

    async def _ask(
        self,
        source: WalletProvider,
        method: str,
        params: list[Any] | None,
        deadline: float | None,
    ) -> Any:
        """One source, given `deadline` seconds to answer."""
        try:
            return await asyncio.wait_for(source.request(method, params), deadline)
        except TimeoutError:
            name = getattr(source, "name", "") or type(source).__name__
            raise WalletError(
                f"{name} did not answer in {deadline:.0f}s."
            ) from None

    async def close(self) -> None:
        """Only the spares. The wallet's session is not this object's to end."""
        for spare in self.spares:
            await spare.close()
