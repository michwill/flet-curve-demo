"""Reading a chain with no wallet: public endpoints, and their failures.

A quote needs no account, so the panels should show rates before anyone
connects anything. What makes that awkward is the endpoints: they are
strangers' machines, they rate-limit, they go away, and one of them will
answer HTML from a captive portal. So the whole of this file is about
what happens when they misbehave -- the happy path is one test.

Nothing here goes near the network. `post_json` and `get_json` are
replaced with scripted fakes, which is also how the ordering guarantees
(where the next request starts, what gets retried and what does not) can
be checked at all.
"""

from __future__ import annotations

import pytest

from curve import rpc
from curve.http import ApiError
from curve.rpc import ChainlistDirectory, PublicNode, usable_endpoints
from wallet.base import RpcError, WalletError

CHAINLIST_PAYLOAD = [
    {
        "chainId": 100,
        "name": "Gnosis",
        "rpc": [
            {"url": "https://tracked.example", "tracking": "yes"},
            {"url": "wss://socket.example"},
            {"url": "https://keyed.example/${API_KEY}", "tracking": "none"},
            {"url": "http://insecure.example", "tracking": "none"},
            {"url": "https://clean.example", "tracking": "none"},
            {"url": "https://plain.example"},
        ],
    },
    {"chainId": 11155111, "name": "Sepolia", "isTestnet": True,
     "rpc": [{"url": "https://sepolia.example"}]},
    {"chainId": 999, "name": "No usable endpoints",
     "rpc": [{"url": "wss://only-a-socket.example"}]},
]


# -- picking endpoints -----------------------------------------------------


def test_only_https_endpoints_this_app_can_call() -> None:
    """Websockets are a different protocol; an API-key template is
    somebody else's key; `http://` will not load on an HTTPS page."""
    urls = usable_endpoints(CHAINLIST_PAYLOAD[0])
    assert urls == [
        "https://clean.example",   # tracking: none, first
        "https://plain.example",   # unstated, second
        "https://tracked.example", # tracking: yes, last
    ]


def test_endpoints_that_keep_nothing_come_first() -> None:
    """`tracking` is self-reported, so this is a nudge, not a guarantee --
    but it is the only privacy signal the list carries."""
    urls = usable_endpoints(CHAINLIST_PAYLOAD[0])
    assert urls[0] == "https://clean.example"
    assert urls[-1] == "https://tracked.example"


def test_the_list_is_capped() -> None:
    """Walking eighty dead hosts is not failover, it is a hang."""
    many = {"rpc": [{"url": f"https://node{i}.example"} for i in range(40)]}
    assert len(usable_endpoints(many)) == rpc.MAX_ENDPOINTS
    assert len(usable_endpoints(many, limit=3)) == 3


# -- the directory ---------------------------------------------------------


@pytest.fixture
def directory(monkeypatch) -> ChainlistDirectory:
    calls: list[str] = []

    async def fake_get_json(url, timeout=None):
        calls.append(url)
        return CHAINLIST_PAYLOAD

    monkeypatch.setattr(rpc, "get_json", fake_get_json)
    made = ChainlistDirectory()
    made.calls = calls  # type: ignore[attr-defined]
    return made


async def test_a_chain_gets_its_endpoints(directory) -> None:
    assert await directory.endpoints(100) == [
        "https://clean.example",
        "https://plain.example",
        "https://tracked.example",
    ]


async def test_the_list_is_fetched_once_for_every_chain(directory) -> None:
    """There is no per-chain endpoint and the file is a couple of
    megabytes, so it is fetched lazily and kept."""
    await directory.endpoints(100)
    await directory.endpoints(100)
    await directory.endpoints(999)
    assert len(directory.calls) == 1


async def test_testnets_are_left_out(directory) -> None:
    assert await directory.endpoints(11155111) == []


async def test_an_unknown_chain_is_empty_not_an_error(directory) -> None:
    assert await directory.endpoints(31337) == []


async def test_a_directory_that_will_not_load_is_not_retried(monkeypatch) -> None:
    """A quote is typed a character at a time; re-fetching two megabytes
    per keystroke because the first attempt failed would be worse than
    having no quote."""
    calls = 0

    async def failing(url, timeout=None):
        nonlocal calls
        calls += 1
        raise ApiError("no")

    monkeypatch.setattr(rpc, "get_json", failing)
    made = ChainlistDirectory()
    assert await made.endpoints(1) == []
    assert await made.endpoints(1) == []
    assert calls == 1


# -- the node --------------------------------------------------------------


class FakeTransport:
    """Scripted `post_json`: per-URL behaviour, and a record of the calls."""

    def __init__(self, behaviour: dict[str, object]) -> None:
        self.behaviour = behaviour
        self.calls: list[str] = []

    async def __call__(self, url, payload, timeout=None):
        self.calls.append(url)
        outcome = self.behaviour.get(url, ApiError(f"{url} is down"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def node_with(monkeypatch, behaviour: dict[str, object], chain_id: int = 100):
    transport = FakeTransport(behaviour)
    monkeypatch.setattr(rpc, "post_json", transport)

    class Fixed(ChainlistDirectory):
        async def endpoints(self, _chain_id):
            return ["https://one.example", "https://two.example", "https://three.example"]

    node = PublicNode(chain_id, Fixed())
    return node, transport


async def test_a_read_comes_back(monkeypatch) -> None:
    node, transport = node_with(monkeypatch, {"https://one.example": {"result": "0x2a"}})
    assert await node.request("eth_blockNumber") == "0x2a"
    assert transport.calls == ["https://one.example"]


async def test_a_dead_endpoint_is_walked_past(monkeypatch) -> None:
    node, transport = node_with(
        monkeypatch,
        {
            "https://one.example": ApiError("timed out"),
            "https://two.example": {"result": "0x7"},
        },
    )
    assert await node.request("eth_chainId") == "0x7"
    assert transport.calls == ["https://one.example", "https://two.example"]


async def test_the_survivor_is_where_the_next_read_starts(monkeypatch) -> None:
    """Otherwise every read pays for the dead host at the top of the list
    again, and a panel makes several per keystroke."""
    node, transport = node_with(
        monkeypatch,
        {
            "https://one.example": ApiError("down"),
            "https://two.example": {"result": "0x1"},
        },
    )
    await node.request("eth_chainId")
    transport.calls.clear()
    await node.request("eth_chainId")
    assert transport.calls == ["https://two.example"]


async def test_something_that_is_not_json_rpc_is_a_failure_too(monkeypatch) -> None:
    """A captive portal answering HTML, or an endpoint that has become a
    web page. It parses as JSON and means nothing."""
    node, transport = node_with(
        monkeypatch,
        {
            "https://one.example": ["not", "an", "object"],
            "https://two.example": {"result": "0x9"},
        },
    )
    assert await node.request("eth_call") == "0x9"


async def test_a_json_rpc_error_is_raised_rather_than_retried(monkeypatch) -> None:
    """A reverted `eth_call` is an *answer*: asking somebody else the same
    question gets the same answer, and retrying would only make a
    reverting quote three times slower."""
    node, transport = node_with(
        monkeypatch,
        {"https://one.example": {"error": {"code": -32000, "message": "execution reverted"}}},
    )
    with pytest.raises(RpcError) as caught:
        await node.request("eth_call")
    assert caught.value.code == -32000
    assert transport.calls == ["https://one.example"]


async def test_every_endpoint_failing_says_so(monkeypatch) -> None:
    node, transport = node_with(monkeypatch, {})
    with pytest.raises(WalletError, match="No public node answered"):
        await node.request("eth_chainId")
    assert len(transport.calls) == 3  # all of them, once each


async def test_a_chain_with_no_endpoints_says_that_instead(monkeypatch) -> None:
    async def nothing(url, payload, timeout=None):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(rpc, "post_json", nothing)

    class Empty(ChainlistDirectory):
        async def endpoints(self, _chain_id):
            return []

    node = PublicNode(31337, Empty())
    with pytest.raises(WalletError, match="No public node is known"):
        await node.request("eth_chainId")


async def test_requests_are_numbered(monkeypatch) -> None:
    """Not required by any endpoint here, but a JSON-RPC id that never
    changes is the sort of thing a batching proxy gets wrong."""
    seen: list[int] = []

    async def transport(url, payload, timeout=None):
        seen.append(payload["id"])
        return {"result": "0x1"}

    monkeypatch.setattr(rpc, "post_json", transport)

    class Fixed(ChainlistDirectory):
        async def endpoints(self, _chain_id):
            return ["https://one.example"]

    node = PublicNode(1, Fixed())
    await node.request("eth_chainId")
    await node.request("eth_chainId")
    assert seen == [1, 2]


async def test_the_node_knows_its_own_chain_without_asking(monkeypatch) -> None:
    """Asking a stranger's endpoint which chain it is would only be a
    chance to disagree with the pool being read."""
    node, transport = node_with(monkeypatch, {}, chain_id=100)
    assert await node.chain_id() == 100
    assert transport.calls == []


# -- what it will not do ---------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda node: node.send_transaction({"to": "0x" + "11" * 20}),
        lambda node: node.sign_message("0x" + "11" * 20, "hello"),
        lambda node: node.switch_chain(1),
    ],
)
async def test_signing_needs_a_wallet(monkeypatch, call) -> None:
    """There is no key here. Saying so beats a transaction that vanishes."""
    node, _transport = node_with(monkeypatch, {})
    with pytest.raises(WalletError, match="Connect a wallet"):
        await call(node)
