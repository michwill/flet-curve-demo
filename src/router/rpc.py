"""Batched JSON-RPC for the router, on this app's own transport.

`erouter.chain` asks for very little -- a batch of requests in, a list of
answers or exceptions out -- and asks for a great deal of it: warming a chain
is a few thousand `eth_getStorageAt` reads, which is a very different shape
from the one-request-at-a-time calls the rest of this app makes.

Three things here were measured on the router's side and are worth keeping:
one request at a time costs 33.6 ms *each* against 68 ms for two hundred in a
batch; a node refuses a batch past its own ceiling and refuses the **whole**
batch, so chunking is not optional; and several chunks in flight beat one,
because the limit is the round trip rather than the node.
"""

from __future__ import annotations

import asyncio

from curve.http import ApiError, post_json

#: Erigon's default, and geth phrases the same refusal.  Chunks are capped
#: here rather than discovered, because discovering it by failing into it
#: costs a whole sweep on the endpoint that has it.
BATCH_LIMIT = 100

#: Chunks in flight at once.  Four measured 3,979 ms serial against 2,334 ms
#: concurrent on the router's own sweep; eight is where the win flattens and
#: is still modest enough that a hosted endpoint has no cause to object.
MAX_STREAMS = 8

#: A sweep is thousands of reads and the endpoint is a load balancer, so a
#: dropped chunk is usually a bad backend rather than a bad request.
RETRIES = 1

#: Long, because a hundred storage reads in one request is a real amount of
#: work for a node and this is not on any interactive path.
TIMEOUT = 60.0


class RouterRpc:
    """The `erouter.chain.evm.AsyncRpc` protocol, over `curve.http`."""

    #: What the session chunks by before it even gets here.
    batch_size = BATCH_LIMIT

    def __init__(self, url: str, chain_id: int, *,
                 max_streams: int = MAX_STREAMS, timeout: float = TIMEOUT):
        self.url = url
        self.chain_id = int(chain_id)
        self._timeout = timeout
        self._streams = asyncio.Semaphore(max_streams)
        self.calls = 0
        self.batches = 0

    async def batch(self, requests) -> list:
        """One answer per request, in order, never raising for one of them.

        An `Exception` in a slot is how a single failure is reported -- the
        same three-state honesty `core.transport.Answer` keeps a level up, and
        for the same reason: a caller has to be able to tell "the node would
        not say" from "the answer is zero".
        """
        requests = list(requests)
        if not requests:
            return []
        chunks = [requests[k:k + BATCH_LIMIT]
                  for k in range(0, len(requests), BATCH_LIMIT)]
        got = await asyncio.gather(*(self._chunk(chunk) for chunk in chunks))
        return [answer for chunk in got for answer in chunk]

    async def call(self, method: str, params: list):
        answer = (await self.batch([(method, params)]))[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def _chunk(self, requests: list) -> list:
        payload = [{"jsonrpc": "2.0", "id": k, "method": method, "params": params}
                   for k, (method, params) in enumerate(requests)]
        last: Exception | None = None
        for _ in range(RETRIES + 1):
            async with self._streams:
                self.batches += 1
                self.calls += len(requests)
                try:
                    answer = await post_json(self.url, payload, timeout=self._timeout)
                except ApiError as exc:
                    last = exc
                    continue
            return _unpack(answer, len(requests), payload)
        return [last or ApiError("the batch was never answered")] * len(requests)


def _unpack(answer, count: int, payload: list) -> list:
    """Match a JSON-RPC batch reply to what was asked, by id.

    A node may answer a batch in any order, and some answer a malformed batch
    with a single object rather than a list -- which read positionally would
    hand every request its neighbour's answer.
    """
    if isinstance(answer, dict):
        answer = [answer]
    if not isinstance(answer, list):
        return [ApiError(f"unexpected batch reply: {type(answer).__name__}")] * count
    by_id = {row.get("id"): row for row in answer if isinstance(row, dict)}
    out: list = []
    for request in payload:
        row = by_id.get(request["id"])
        if row is None:
            out.append(ApiError(f"no answer for {request['method']}"))
        elif "error" in row:
            out.append(ApiError(str(row["error"])[:120]))
        else:
            out.append(row.get("result"))
    return out
