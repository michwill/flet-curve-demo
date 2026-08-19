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

import asyncio

import pytest

from curve import rpc
from curve.http import ApiError
from curve.rpc import (
    ChainlistDirectory,
    FallbackProvider,
    PublicNode,
    prefers_public_reads,
    usable_endpoints,
)
from wallet.base import RpcError, WalletError, WalletProvider

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


async def test_a_directory_that_will_not_load_is_not_retried_at_once(monkeypatch) -> None:
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


async def test_a_directory_that_failed_is_tried_again_later(monkeypatch) -> None:
    """The wait is a wait, not a surrender.

    It used to be permanent, and that made one failed fetch cost the
    whole session: no endpoints for any chain until the page was
    reloaded, every read failing, and the pool panel reporting it as a
    pool with no parameters. Nothing here can work without this file, so
    it gets asked again.
    """
    attempts = 0

    async def failing_once(url, timeout=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ApiError("no")
        return CHAINLIST_PAYLOAD

    clock = [1000.0]
    monkeypatch.setattr(rpc, "get_json", failing_once)
    monkeypatch.setattr(rpc.time, "monotonic", lambda: clock[0])

    made = ChainlistDirectory()
    assert await made.endpoints(100) == []

    clock[0] += rpc.RETRY_AFTER - 1  # still inside the wait
    assert await made.endpoints(100) == []
    assert attempts == 1

    clock[0] += 2  # past it
    assert await made.endpoints(100) == ["https://clean.example",
                                         "https://plain.example",
                                         "https://tracked.example"]
    assert attempts == 2


async def test_a_directory_that_answers_junk_is_tried_again_too(monkeypatch) -> None:
    """A captive portal, or a gateway serving its own error page. It
    parses and it is not the list, which is a failed load like any
    other rather than a directory with no chains in it."""
    served: list[object] = [{"error": "nope"}, CHAINLIST_PAYLOAD]

    async def answering(url, timeout=None):
        return served.pop(0)

    clock = [0.0]
    monkeypatch.setattr(rpc, "get_json", answering)
    monkeypatch.setattr(rpc.time, "monotonic", lambda: clock[0])

    made = ChainlistDirectory()
    assert await made.endpoints(100) == []
    clock[0] += rpc.RETRY_AFTER
    assert (await made.endpoints(100))[:1] == ["https://clean.example"]


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
    node, _ = node_with(
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
    node, _ = node_with(monkeypatch, {})
    with pytest.raises(WalletError, match="Connect a wallet"):
        await call(node)


# -- a wallet with the public endpoints behind it --------------------------
#
# The bug: a portfolio scan is a Multicall3 batch of three hundred entries,
# and pushing that through a WalletConnect relay into a phone failed with
# WebKit's "Load failed". The wallet was the only thing asked, so the page
# said it could not read the chain and stopped. It should have asked
# somebody else.


class Scripted(WalletProvider):
    """A provider that answers, or fails, exactly as told."""

    def __init__(self, *answers: object, name: str = "scripted") -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, object]] = []
        self.name = name
        self.closed = False

    async def request(self, method: str, params=None):
        self.calls.append((method, params))
        answer = self.answers.pop(0) if self.answers else "0x"
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self) -> None:
        self.closed = True


def test_a_reader_presents_itself_as_the_wallet_it_wraps() -> None:
    """The UI names the transport from the provider, so wrapping one for
    failover must not rename the user's wallet to something else."""
    wallet = Scripted(name="Rabby")
    wallet.kind, wallet.connector = "browser", "walletconnect"
    reader = FallbackProvider(wallet, Scripted())

    assert (reader.name, reader.kind, reader.connector) == (
        "Rabby",
        "browser",
        "walletconnect",
    )


async def test_a_read_the_wallet_cannot_carry_goes_to_a_public_node() -> None:
    """The reported failure, in one test: the relay would not carry the
    batch, so the answer has to come from somewhere else."""
    wallet = Scripted(WalletError("Load failed"))
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"
    assert wallet.calls and public.calls, "both were asked, in that order"


async def test_the_wallet_is_asked_first_and_alone_when_it_answers() -> None:
    """Its node is the one the transaction will run on, so nothing else
    is troubled when it works."""
    wallet = Scripted("0xf00d")
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xf00d"
    assert public.calls == [], "the spare was not needed"


async def test_a_node_that_answered_no_is_not_asked_again_elsewhere() -> None:
    """A reverted call is a reply. Asking a different endpoint the same
    question gets the same reply, more slowly -- and a user rejection
    asked twice is a second prompt."""
    wallet = Scripted(RpcError(3, "execution reverted"))
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public)

    with pytest.raises(RpcError):
        await reader.call("0x" + "ca11" * 10, "0xdata")
    assert public.calls == [], "a revert is not a transport failure"


@pytest.mark.parametrize(
    "method, params",
    [
        ("eth_sendTransaction", [{"to": "0x" + "11" * 20}]),
        ("personal_sign", ["0xdead", "0x" + "11" * 20]),
        ("wallet_switchEthereumChain", [{"chainId": "0x1"}]),
    ],
)
async def test_only_reads_ever_leave_the_wallet(method, params) -> None:
    """There is no fallback for a signature, and a public node could not
    provide one. A write goes to the user's wallet or it fails."""
    wallet = Scripted(WalletError("wallet is being difficult"))
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public)

    with pytest.raises(WalletError, match="difficult"):
        await reader.request(method, params)
    assert public.calls == [], "a public node must never be asked to sign"


async def test_the_last_failure_is_what_the_user_is_told() -> None:
    """When nothing answers, the message has to come from somewhere, and
    the final attempt is the most recent thing that actually happened."""
    reader = FallbackProvider(
        Scripted(WalletError("Load failed")),
        Scripted(WalletError("no public node answered")),
    )
    with pytest.raises(WalletError, match="no public node answered"):
        await reader.call("0x" + "ca11" * 10, "0xdata")


class Silent(WalletProvider):
    """A provider that accepts a request and never answers it.

    Which is what a wallet whose node has gone away looks like from here:
    the bridge posts the message, nothing comes back, and the deadline is
    the only thing that ends the wait.
    """

    def __init__(self, name: str = "silent") -> None:
        self.name = name
        self.calls: list[str] = []

    async def request(self, method: str, params=None):
        self.calls.append(method)
        await asyncio.Event().wait()

    async def close(self) -> None: ...


async def test_a_wallet_that_never_answers_is_stepped_over(monkeypatch) -> None:
    """The bug this exists for: pool parameters that never load.

    A wallet gets 120 seconds for a request -- right for a signature and
    absurd for an `eth_call` -- so a wallet connected to a node that has
    gone away used to hold every read for two minutes with a public
    endpoint sitting behind it. Thirteen of those in one panel is
    twenty-eight minutes, which reads as broken rather than as slow.
    """
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)
    wallet, public = Silent(), Scripted("0xbeef")
    reader = FallbackProvider(wallet, public)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"
    assert wallet.calls, "it was asked first, as always"


async def test_the_dead_wallet_is_not_waited_on_again(monkeypatch) -> None:
    """`parameters()` is one multicall and thirteen single reads behind
    it. Paying the deadline on each is the same hang, thirteen times
    shorter -- so the first failure is remembered."""
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)
    wallet = Silent()
    public = Scripted(*["0xbeef"] * 14)
    reader = FallbackProvider(wallet, public)

    for _ in range(14):
        assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"

    assert len(wallet.calls) == 1, "asked once, then left out of the order"
    assert len(public.calls) == 14


async def test_a_wallet_that_comes_back_is_asked_again(monkeypatch) -> None:
    """A laptop off standby, a phone that reconnected. The wallet is the
    node the transaction will run on, so it gets its place back rather
    than being written off for the session."""
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)
    monkeypatch.setattr(rpc, "SOURCE_COOLDOWN", 0.0)
    wallet = Scripted(WalletError("Load failed"), "0xf00d")
    public = Scripted("0xbeef", "0xbeef")
    reader = FallbackProvider(wallet, public)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"
    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xf00d"


async def test_the_last_source_is_waited_on_however_long_it_takes(
    monkeypatch,
) -> None:
    """A deadline is only worth having when there is somewhere to go. On
    the last source it converts a slow answer into no answer."""
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)
    slow = Scripted("0xbeef")
    real_request = slow.request

    async def dawdle(method, params=None):
        await asyncio.sleep(0.05)
        return await real_request(method, params)

    slow.request = dawdle  # type: ignore[method-assign]
    reader = FallbackProvider(slow)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"


async def test_every_source_cold_is_still_a_reason_to_try(monkeypatch) -> None:
    """An empty read order is not a reason to fail without asking: it is
    the moment to find out whether one has come back."""
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)
    wallet = Scripted(WalletError("gone"), "0xf00d")
    public = Scripted(WalletError("gone"), "0xbeef")
    reader = FallbackProvider(wallet, public)

    with pytest.raises(WalletError):
        await reader.call("0x" + "ca11" * 10, "0xdata")
    assert reader.read_order() == [0, 1], "both cold, so both are asked again"
    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xf00d"


async def test_a_signature_still_waits_on_the_human(monkeypatch) -> None:
    """The deadline is for reads. A send waits on somebody reading a
    prompt, and eight seconds is not how long that takes."""
    monkeypatch.setattr(rpc, "READ_DEADLINE", 0.01)

    async def slow_send(method, params=None):
        await asyncio.sleep(0.05)
        return "0xhash"

    wallet = Scripted()
    wallet.request = slow_send  # type: ignore[method-assign]
    reader = FallbackProvider(wallet, Scripted("0xbeef"))

    assert await reader.request("eth_sendTransaction", [{}]) == "0xhash"


async def test_closing_a_reader_leaves_the_wallet_session_alone() -> None:
    """The wallet outlives any one page's reads, and its session is the
    user's, not this object's, to end."""
    wallet, public = Scripted(), Scripted()
    await FallbackProvider(wallet, public).close()

    assert not wallet.closed, "disconnecting the user's wallet is not our call"
    assert public.closed


async def test_closing_still_spares_a_wallet_read_last() -> None:
    """The wallet is not identified by its position in the read order."""
    wallet, public = Scripted(), Scripted()
    await FallbackProvider(wallet, public, spares_first=True).close()

    assert not wallet.closed
    assert public.closed


# -- reading past a relay rather than through it ----------------------------


def test_only_walletconnect_wants_its_reads_taken_elsewhere() -> None:
    """An injected wallet is a node in the same browser; there is no
    relay to route around and its own view is the better one."""
    relayed = Scripted()
    relayed.connector = "walletconnect"
    injected = Scripted()
    injected.connector = ""

    assert prefers_public_reads(relayed)
    assert not prefers_public_reads(injected)
    assert not prefers_public_reads(Scripted())


async def test_a_relayed_wallet_is_read_past_not_through() -> None:
    """The sharper half of the fix: do not send six Multicall3 batches
    into a phone at all, rather than sending them and recovering."""
    wallet = Scripted("0xf00d")
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public, spares_first=True)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xbeef"
    assert wallet.calls == [], "the phone was not disturbed"


async def test_a_relayed_wallet_is_still_asked_when_nothing_public_answers() -> None:
    """On a chain chainlist has never heard of, the wallet is the only
    thing that can answer at all. Preferring the public nodes must not
    mean refusing to fall back to the wallet."""
    wallet = Scripted("0xf00d")
    public = Scripted(WalletError("No public node is known for this network"))
    reader = FallbackProvider(wallet, public, spares_first=True)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0xf00d"


async def test_a_relayed_wallet_is_still_the_only_thing_that_signs() -> None:
    """Reading past the wallet must not become sending past it: a public
    node has no key, and reordering reads is not a licence to reorder
    anything else."""
    wallet = Scripted("0xhash")
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public, spares_first=True)

    assert await reader.request("eth_sendTransaction", [{"to": "0x0"}]) == "0xhash"
    assert public.calls == []


# -- a wallet that is on another chain --------------------------------------
#
# Not a slow source or a flaky one. A source that answers the wrong
# question confidently, which no amount of falling back can detect.


async def test_a_wallet_on_another_chain_is_not_read_through_at_all() -> None:
    """The reported failure. `balanceOf` on an Ethereum LP token, asked of
    a wallet sitting on Fraxtal, returns `0x` -- there is no code at that
    address there. Successfully. So nothing raises, nothing falls back,
    and the portfolio reads empty for an account with eight positions.

    The only fix is not to ask.
    """
    wallet = Scripted("0x")  # what a call to a contract that is not there says
    public = Scripted("0x" + "0" * 63 + "5")
    reader = FallbackProvider(wallet, public, read_primary=False)

    assert await reader.call("0x" + "ca11" * 10, "0xdata") == "0x" + "0" * 63 + "5"
    assert wallet.calls == [], "asked, it would have answered, and been wrong"


async def test_a_wallet_on_another_chain_is_still_the_one_that_signs() -> None:
    """Which is the whole reason it is dropped from `sources` rather than
    from the object: the wallet that must not be read through is exactly
    the wallet that must be asked to move."""
    wallet = Scripted("0xhash", "0xhash")
    public = Scripted("0xbeef")
    reader = FallbackProvider(wallet, public, read_primary=False)

    assert await reader.request("eth_sendTransaction", [{"to": "0x0"}]) == "0xhash"
    assert await reader.request("wallet_switchEthereumChain", [{"chainId": "0x1"}])
    assert public.calls == []


async def test_which_chain_it_is_on_is_still_asked_of_the_wallet() -> None:
    """`eth_chainId` is not a read of the chain, so it never went through
    `sources` -- which is what keeps this recoverable. The app can still
    see where the wallet is, and so can still ask it to come across."""
    wallet = Scripted("0xfc")  # Fraxtal
    public = Scripted("0x1")
    reader = FallbackProvider(wallet, public, read_primary=False)

    assert await reader.chain_id() == 252
    assert public.calls == []


async def test_closing_spares_a_wallet_that_was_never_read_from() -> None:
    """Same rule as everywhere else here: the session is the user's."""
    wallet, public = Scripted(), Scripted()
    await FallbackProvider(wallet, public, read_primary=False).close()

    assert not wallet.closed
    assert public.closed


# -- the question that is about the source, not the chain -------------------


@pytest.mark.parametrize("spares_first", [False, True])
async def test_which_chain_the_wallet_is_on_is_asked_of_the_wallet(
    spares_first,
) -> None:
    """`eth_chainId` does not read the chain -- it asks a source which
    chain it is on, and each source here has a different honest answer.

    The action panel uses it to decide whether the wallet needs switching
    networks before it can act. Answered by a public node, pinned to the
    chain the app is showing, that check would pass every time and the
    "switch your wallet" prompt would never appear again.
    """
    wallet = Scripted("0xa")  # the wallet is on Optimism
    public = Scripted("0x1")  # the public node is Ethereum's
    reader = FallbackProvider(wallet, public, spares_first=spares_first)

    assert await reader.chain_id() == 10
    assert public.calls == [], "a public node cannot answer this for a wallet"


async def test_a_wallet_that_cannot_say_which_chain_is_not_guessed_for() -> None:
    """Failing over here would invent an answer: the public node would
    happily report the chain it is pinned to, which says nothing about
    where the user's wallet is pointed."""
    reader = FallbackProvider(
        Scripted(WalletError("Load failed")), Scripted("0x1")
    )
    with pytest.raises(WalletError, match="Load failed"):
        await reader.chain_id()
