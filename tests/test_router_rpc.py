"""The router's transport, and what it does when a backend goes quiet.

The endpoint is a load balancer in front of many nodes, and the failure that
actually reached a reader was not an error: one backend simply never answered,
and a chunk sat on it until the timeout.  These are the rules that keep that
from being a page that does not load.
"""

from __future__ import annotations

import asyncio

import pytest

import router.rpc as rpc_module
from curve.http import ApiError
from router.rpc import ATTEMPTS, BATCH_LIMIT, RouterRpc

pytestmark = pytest.mark.asyncio


def answer(payload):
    """A well-formed reply to whatever was asked."""
    return [{"jsonrpc": "2.0", "id": row["id"], "result": "0x1"} for row in payload]


class Endpoint:
    """A stand-in whose every post is scripted, and which records them."""

    def __init__(self, *script) -> None:
        #: One entry per post: a delay in seconds, or an exception to raise.
        self.script = list(script)
        self.posts: list[list] = []

    async def post(self, url, payload, timeout=0.0):
        turn = self.script[min(len(self.posts), len(self.script) - 1)]
        self.posts.append(payload)
        if isinstance(turn, Exception):
            raise turn
        await asyncio.sleep(turn)
        return answer(payload)


def endpoint(monkeypatch, *script, hedge_after=0.02) -> Endpoint:
    """Point `router.rpc` at a scripted endpoint, hedging almost at once."""
    fake = Endpoint(*script)
    monkeypatch.setattr(rpc_module, "post_json", fake.post)
    monkeypatch.setattr(rpc_module, "HEDGE_AFTER", hedge_after)
    return fake


async def test_a_chunk_that_answers_is_not_sent_twice(monkeypatch) -> None:
    """Hedging is for silence. A healthy endpoint must never see a duplicate."""
    fake = endpoint(monkeypatch, 0.0)
    rpc = RouterRpc("https://node", 1)

    got = await rpc.batch([("eth_getBalance", ["0x0", "latest"])])

    assert got == ["0x1"]
    assert len(fake.posts) == 1
    assert rpc.hedged == 0


async def test_a_backend_that_goes_quiet_is_overtaken(monkeypatch) -> None:
    """The one that reached a reader: no error, no refusal, just silence --
    and the chunk sat on it for the whole timeout before trying again."""
    fake = endpoint(monkeypatch, 30.0, 0.0)          # first hangs, second answers
    rpc = RouterRpc("https://node", 1, timeout=30.0)

    got = await asyncio.wait_for(
        rpc.batch([("eth_getBalance", ["0x0", "latest"])]), timeout=5.0
    )

    assert got == ["0x1"]
    assert len(fake.posts) == 2
    assert rpc.hedged == 1


async def test_the_first_answer_wins_and_the_rest_are_dropped(monkeypatch) -> None:
    fake = endpoint(monkeypatch, 30.0, 30.0, 0.0)
    rpc = RouterRpc("https://node", 1, timeout=30.0)

    got = await asyncio.wait_for(rpc.batch([("eth_chainId", [])]), timeout=5.0)
    pending = [t for t in asyncio.all_tasks() if "post" in str(t.get_coro())]

    assert got == ["0x1"]
    assert len(fake.posts) == ATTEMPTS
    assert not pending


async def test_a_refusal_is_retried_without_waiting_for_the_hedge(monkeypatch)\
        -> None:
    """An outright failure has already told us this backend is no good."""
    fake = endpoint(monkeypatch, ApiError("HTTP 503"), 0.0, hedge_after=30.0)
    rpc = RouterRpc("https://node", 1)

    got = await asyncio.wait_for(rpc.batch([("eth_chainId", [])]), timeout=5.0)

    assert got == ["0x1"]
    assert len(fake.posts) == 2


async def test_every_attempt_failing_is_an_error_per_request(monkeypatch) -> None:
    """One exception per slot, never a raise: the caller has to be able to
    tell "the node would not say" from "the answer is zero"."""
    fake = endpoint(monkeypatch, ApiError("HTTP 503"), hedge_after=30.0)
    rpc = RouterRpc("https://node", 1)

    got = await rpc.batch([("eth_chainId", []), ("eth_blockNumber", [])])

    assert len(fake.posts) == ATTEMPTS
    assert all(isinstance(a, ApiError) for a in got)
    assert "503" in str(got[0])


async def test_a_batch_past_the_size_is_split_and_every_answer_comes_back(
        monkeypatch) -> None:
    endpoint(monkeypatch, 0.0)
    rpc = RouterRpc("https://node", 1)
    requests = [("eth_getStorageAt", [f"0x{k:040x}", "0x0", "latest"])
                for k in range(BATCH_LIMIT * 2 + 5)]

    got = await rpc.batch(requests)

    assert len(got) == len(requests)
    assert all(a == "0x1" for a in got)


async def test_the_timeout_is_the_last_resort_not_the_working_bound() -> None:
    """`HEDGE_AFTER` is what a stalled backend costs; the timeout only bounds
    the case where every backend is stalled."""
    from router.rpc import HEDGE_AFTER, TIMEOUT

    assert HEDGE_AFTER < TIMEOUT / 5
    assert ATTEMPTS >= 2
