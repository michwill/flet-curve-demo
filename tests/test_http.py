"""What the two HTTP halves put on the wire."""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

from curve import http

# -- the browser half ------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def json(self) -> object:
        return self.payload


@pytest.fixture
def pyfetch(monkeypatch):
    """Stand in for `pyodide.http.pyfetch` and record every call."""
    seen: list[dict] = []

    async def fake_pyfetch(url, **kwargs):
        seen.append({"url": url, **kwargs})
        return FakeResponse({"ok": True})

    module = types.ModuleType("pyodide.http")
    module.pyfetch = fake_pyfetch  # type: ignore[attr-defined]
    package = types.ModuleType("pyodide")
    package.http = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyodide", package)
    monkeypatch.setitem(sys.modules, "pyodide.http", module)
    monkeypatch.setattr(http, "is_browser", lambda: True)
    return seen


async def test_a_browser_get_sends_no_headers_at_all(pyfetch) -> None:
    assert await http.get_json("https://chainlist.org/rpcs.json") == {"ok": True}
    assert pyfetch[0]["url"] == "https://chainlist.org/rpcs.json"
    assert "headers" not in pyfetch[0] or not pyfetch[0]["headers"]


async def test_a_browser_post_sends_content_type_and_nothing_else(pyfetch) -> None:
    await http.post_json("https://eth.example", {"method": "eth_call"})
    headers = pyfetch[0]["headers"]
    assert headers == {"Content-Type": "application/json"}


@pytest.mark.parametrize(
    "call",
    [
        lambda: http.get_json("https://chainlist.org/rpcs.json"),
        lambda: http.post_json("https://eth.example", {"method": "eth_call"}),
    ],
)
async def test_the_browser_never_names_a_user_agent(pyfetch, call) -> None:
    await call()
    headers = {k.lower() for k in (pyfetch[0].get("headers") or {})}
    assert "user-agent" not in headers


async def test_a_browser_post_still_sends_the_body(pyfetch) -> None:
    await http.post_json("https://eth.example", {"method": "eth_chainId", "id": 7})
    assert json.loads(pyfetch[0]["body"]) == {"method": "eth_chainId", "id": 7}
    assert pyfetch[0]["method"] == "POST"


async def test_a_browser_http_error_is_an_api_error(pyfetch, monkeypatch) -> None:
    async def failing(url, **kwargs):
        return FakeResponse(None, status=503)

    sys.modules["pyodide.http"].pyfetch = failing  # type: ignore[attr-defined]
    with pytest.raises(http.ApiError, match="HTTP 503"):
        await http.get_json("https://chainlist.org/rpcs.json")


async def test_a_stalled_bundle_fetch_gives_up_instead_of_hanging(monkeypatch) -> None:
    """It used to have no deadline at all, and `load_pools` awaits this
    before drawing the first page of rows -- so a connection that was
    accepted and never answered meant no pools, ever. A gateway that 504s
    raises; one that goes quiet did not."""
    import asyncio

    async def never(url):
        await asyncio.Event().wait()

    monkeypatch.setattr(http, "is_browser", lambda: True)
    monkeypatch.setattr(http, "_browser_bytes", never)

    with pytest.raises(http.ApiError, match="Timed out"):
        await http.get_bytes("https://curve.eth.limo/curve/chains/marks@80.bin", timeout=0.01)


async def test_bytes_that_arrive_are_handed_back(monkeypatch) -> None:
    async def quick(url):
        return b"\x89PNG"

    monkeypatch.setattr(http, "is_browser", lambda: True)
    monkeypatch.setattr(http, "_browser_bytes", quick)

    assert await http.get_bytes("https://x/marks@80.bin") == b"\x89PNG"


# -- the desktop half ------------------------------------------------------


class FakeUrlopen:
    """Records the `Request` it was handed and answers with JSON."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b'{"ok": true}'


class FakeConnection:
    """One kept-alive connection, recording what was asked of it."""

    def __init__(self, host, port=None, timeout=None, context=None) -> None:
        self.host = host
        self.sock = None            # never reused, so no retry is attempted
        self.asked: list[tuple] = []
        self.closed = False

    def request(self, method, target, body=None, headers=None) -> None:
        self.asked.append((method, target, body, dict(headers or {})))
        self.sock = object()

    def getresponse(self):
        return FakeAnswer()

    def close(self) -> None:
        self.closed = True


class FakeAnswer:
    status = 200
    will_close = False

    def read(self) -> bytes:
        return b'{"ok": true}'

    def getheader(self, _name, default=None):
        return default


@pytest.fixture
def connections(monkeypatch):
    """Every desktop request, without a socket in sight.

    `http` in this file is `curve.http`, so the standard library's goes
    through `import_module` rather than a bare import that would find ours.
    """
    client = importlib.import_module("http.client")
    made: list[FakeConnection] = []

    def build(*args, **kwargs):
        made.append(FakeConnection(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(client, "HTTPSConnection", build)
    monkeypatch.setattr(client, "HTTPConnection", build)
    monkeypatch.setattr(http, "is_browser", lambda: False)
    http.forget_connections()
    yield made
    http.forget_connections()


async def test_the_desktop_half_still_names_itself(connections) -> None:
    """Curve's edge answers 403 to the default `Python-urllib/x.y`, so the
    app's own agent has to survive the move off `urlopen`."""
    await http.get_json("https://api.curve.finance/api/getPools/all/ethereum")
    _method, _target, _body, headers = connections[0].asked[0]
    assert headers["User-Agent"] == http.USER_AGENT


async def test_the_desktop_half_asks_to_keep_the_connection(connections) -> None:
    """The whole point of the move: six page reads took 7.85s opening a
    connection each time and 1.05s over one kept open."""
    await http.get_json("https://api.curve.finance/one")
    _method, _target, _body, headers = connections[0].asked[0]
    assert headers["Connection"] == "keep-alive"


async def test_a_second_request_to_a_host_reuses_the_connection(connections) -> None:
    await http.get_json("https://api.curve.finance/one")
    await http.get_json("https://api.curve.finance/two")

    assert len(connections) == 1, "the second went over the first's socket"
    assert [asked[1] for asked in connections[0].asked] == ["/one", "/two"]


async def test_a_different_host_gets_its_own_connection(connections) -> None:
    await http.get_json("https://api.curve.finance/one")
    await http.get_json("https://prices.curve.finance/two")

    assert len(connections) == 2, "a connection is to a host, not to a URL"


async def test_the_query_string_goes_with_the_path(connections) -> None:
    """`http.client` takes a target rather than a URL, so a query dropped here
    would be a request for the wrong thing that still answered 200."""
    await http.get_json("https://api.curve.finance/pools?chain=1&page=2")
    assert connections[0].asked[0][1] == "/pools?chain=1&page=2"


async def test_the_desktop_post_names_itself_too(connections) -> None:
    await http.post_json("https://eth.example", {"method": "eth_call"})
    method, _target, body, headers = connections[0].asked[0]
    assert method == "POST"
    assert body == b'{"method": "eth_call"}'
    assert headers["User-Agent"] == http.USER_AGENT
    assert headers["Content-Type"] == "application/json"


class DroppingConnection(FakeConnection):
    """A connection whose far end hung up while it sat in the pool."""

    drop_when_reused = True

    def request(self, method, target, body=None, headers=None):
        if self.sock is not None and self.drop_when_reused:
            client = importlib.import_module("http.client")
            raise client.RemoteDisconnected("closed by the far end")
        return super().request(method, target, body, headers)


async def test_a_connection_the_far_end_dropped_is_tried_again(monkeypatch):
    """Keep-alive's one hazard: nothing says a pooled socket is dead until
    the write fails.  The caller must not see that."""
    client = importlib.import_module("http.client")
    made: list[DroppingConnection] = []

    def build(*args, **kwargs):
        made.append(DroppingConnection(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(client, "HTTPSConnection", build)
    monkeypatch.setattr(http, "is_browser", lambda: False)
    http.forget_connections()
    try:
        await http.get_json("https://api.curve.finance/one")   # opens
        got = await http.get_json("https://api.curve.finance/two")  # reuses, dies
    finally:
        http.forget_connections()

    assert got == {"ok": True}, "the retry answered"
    assert len(made) == 2, "a fresh socket, not the dead one"
    assert made[0].closed, "and the dead one was closed rather than pooled"


async def test_a_fresh_connection_that_fails_is_not_retried(monkeypatch):
    """One retry, and only for a socket that had been used.  Retrying a brand
    new connection would double every real outage."""
    client = importlib.import_module("http.client")
    made: list[FakeConnection] = []

    class Refusing(FakeConnection):
        def request(self, method, target, body=None, headers=None):
            raise OSError("connection refused")

    def build(*args, **kwargs):
        made.append(Refusing(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(client, "HTTPSConnection", build)
    monkeypatch.setattr(http, "is_browser", lambda: False)
    http.forget_connections()
    try:
        with pytest.raises(http.ApiError):
            await http.get_json("https://api.curve.finance/one")
    finally:
        http.forget_connections()

    assert len(made) == 1, "tried once, not twice"


async def test_a_host_that_asks_to_close_is_not_kept(connections) -> None:
    """`Connection: close` is the server saying so; pooling it anyway would
    make the next caller pay for a retry to find out."""
    class Closing(FakeAnswer):
        will_close = True

    monkeypatch_answer = Closing
    connections_before = len(connections)
    original = FakeConnection.getresponse
    FakeConnection.getresponse = lambda self: monkeypatch_answer()
    try:
        await http.get_json("https://api.curve.finance/one")
        await http.get_json("https://api.curve.finance/two")
    finally:
        FakeConnection.getresponse = original

    assert len(connections) - connections_before == 2, "neither was kept"
