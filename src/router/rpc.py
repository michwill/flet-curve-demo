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

#: Erigon's default, and geth phrases the same refusal.  The floor every
#: endpoint answers, and where chunking starts before `probe` has run.
BATCH_LIMIT = 100

#: What to ask for, largest first.  drpc serves 2,000 and Erigon refuses the
#: *whole* batch past 100, so assuming the floor is twenty times the round
#: trips on the endpoint this actually ships with -- 62 against 4 on one
#: mainnet sweep.  Worth one request to find out which it is.
BATCH_LADDER = (2000, 1000, 500, 200, BATCH_LIMIT)

#: Chunks in flight at once.  Four measured 3,979 ms serial against 2,334 ms
#: concurrent on the router's own sweep; eight is where the win flattens and
#: is still modest enough that a hosted endpoint has no cause to object.
#: Read by the caller too, to decide how much to hand over at a time -- less
#: than `batch_size * max_streams` and most of these sit idle.
MAX_STREAMS = 8

#: How long to let a chunk go unanswered before posting it a second time.
#: Measured over 615 chunks of one mainnet warm: 78 ms median, 169 ms at the
#: 95th, 1.4 s the worst of them.  Two seconds is past all of that, so a
#: healthy endpoint never hedges and a silent one is caught quickly.
HEDGE_AFTER = 2.0

#: Posts of the same chunk before giving up, the first included.  The endpoint
#: is a load balancer, so a second post is a second backend.
ATTEMPTS = 3

#: The last-resort bound, not the working one -- `HEDGE_AFTER` is what a
#: stalled backend actually costs.  Generous enough that a slow connection
#: carrying two thousand reads is not cut off, where 60 s meant a page that
#: waited two minutes on one dead backend and then gave up.
TIMEOUT = 20.0


class RouterRpc:
    """The `erouter.chain.evm.AsyncRpc` protocol, over `curve.http`."""

    def __init__(self, url: str, chain_id: int, *,
                 max_streams: int = MAX_STREAMS, timeout: float = TIMEOUT):
        self.url = url
        self.chain_id = int(chain_id)
        self._timeout = timeout
        #: What one request may carry.  The floor until `probe` says better;
        #: read by the caller to size what it hands over.
        self.batch_size = BATCH_LIMIT
        self.max_streams = int(max_streams)
        self._streams = asyncio.Semaphore(max_streams)
        self.calls = 0
        self.batches = 0
        #: How many chunks needed a second post. Zero on a healthy endpoint,
        #: and the number worth watching when one starts misbehaving.
        self.hedged = 0

    async def probe(self) -> int:
        """Ask this endpoint how much it will take in one request.

        With the method actually about to be sent, not a cheap stand-in: a
        node may cap by payload size or by method, and a ceiling learned from
        `eth_blockNumber` would not survive contact with a storage sweep.

        A refusal is the answer, so nothing here is an error -- the floor is
        already known to work.
        """
        sample = ("eth_getStorageAt", [_PROBE_ADDRESS, _PROBE_SLOT, "latest"])
        for size in BATCH_LADDER:
            if size <= self.batch_size and self.batch_size != BATCH_LIMIT:
                break
            got = await self._chunk([sample] * size)
            if len(got) == size and not any(isinstance(a, Exception) for a in got):
                self.batch_size = size
                return size
        return self.batch_size

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
        size = max(1, self.batch_size)
        chunks = [requests[k:k + size] for k in range(0, len(requests), size)]
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
        async with self._streams:
            self.calls += len(requests)
            try:
                answer = await self._raced(payload)
            except ApiError as exc:
                return [exc] * len(requests)
        return _unpack(answer, len(requests), payload)

    async def _raced(self, payload: list):
        """Post it, and post it again if the first has not answered in time.

        A chunk still silent after `HEDGE_AFTER` is not slow, it is on a
        backend that is never going to answer -- the median chunk comes back
        in 78 ms.  Retrying only once the timeout expires meant sitting on a
        dead backend for a minute and then starting over, which is a swap page
        that does not load.

        So the attempts overlap: whichever backend speaks first wins and the
        rest are dropped.  Safe to send twice because every request the router
        makes is a read, and the endpoint's own key allows nothing else.
        """
        running: set[asyncio.Task] = set()
        last: BaseException | None = None
        try:
            for attempt in range(ATTEMPTS):
                if attempt:
                    self.hedged += 1
                self.batches += 1
                running.add(asyncio.create_task(
                    post_json(self.url, payload, timeout=self._timeout)))
                # The last attempt has nothing left to hedge with, so it waits
                # on the timeout rather than on the hedge interval.
                patience = HEDGE_AFTER if attempt < ATTEMPTS - 1 else None
                while running:
                    done, running = await asyncio.wait(
                        running, timeout=patience,
                        return_when=asyncio.FIRST_COMPLETED)
                    if not done:
                        break            # still silent: send another
                    for task in done:
                        failed = task.exception()
                        if failed is None:
                            return task.result()
                        last = failed
                    if not running:
                        break            # every one so far refused: send another
        finally:
            for task in running:
                task.cancel()
        raise last or ApiError("the batch was never answered")


#: The zero account, for asking an endpoint what size of batch it will take.
#: Any address does -- what is being measured is the batch, not the answer.
_PROBE_ADDRESS = "0x" + "00" * 20
_PROBE_SLOT = "0x" + "00" * 32


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
