"""What the two HTTP halves put on the wire.

Almost all of this file is about one header. `User-Agent` has to be sent
from the desktop half -- Curve's edge answers 403 to `Python-urllib/x.y`
-- and must *not* be sent from the browser half, and the reason it must
not is invisible from Python: it decides whether a cross-origin request
is "simple" or needs the host's permission first.

That asymmetry cost a day. Chrome strips `User-Agent` from `fetch`, so
the request stayed simple and the browser build worked on a desktop.
WebKit and Firefox follow the current standard and send it, which made
every request non-simple; `chainlist.org` answers the resulting preflight
without naming `user-agent`, so the endpoint directory would not load, so
no chain could be read at all. On a phone -- where every browser is
WebKit, Brave on iOS included -- pool parameters came up empty and said
the pool had none.

None of that is reproducible from CPython, so what is pinned here is the
one thing that is: which headers each half asks for.
"""

from __future__ import annotations

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
    """Stand in for `pyodide.http.pyfetch` and record every call.

    The import is inside the function under test, so this goes in through
    `sys.modules` -- there is no Pyodide here to import.
    """
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
    """Which is what keeps it a "simple" cross-origin request: no
    preflight to fail, and nothing for a host to refuse."""
    assert await http.get_json("https://chainlist.org/rpcs.json") == {"ok": True}
    assert pyfetch[0]["url"] == "https://chainlist.org/rpcs.json"
    assert "headers" not in pyfetch[0] or not pyfetch[0]["headers"]


async def test_a_browser_post_sends_content_type_and_nothing_else(pyfetch) -> None:
    """JSON-RPC needs `Content-Type`, and that one already costs a
    preflight -- every endpoint serving browsers answers it. `User-Agent`
    on top would ask permission for a header many of them do not name."""
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
    """The whole bug in one assertion. A browser sends its own and will
    not let a page forge one, so nothing is lost -- but naming it makes
    the request non-simple, and that is what broke iOS."""
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


@pytest.fixture
def urlopen(monkeypatch):
    import urllib.request

    fake = FakeUrlopen()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(http, "is_browser", lambda: False)
    return fake


async def test_the_desktop_half_still_names_itself(urlopen) -> None:
    """`urllib` has no browser to name it, and Curve's edge blocks the
    literal default `Python-urllib/x.y`. This is the header's only
    remaining job."""
    await http.get_json("https://api.curve.finance/api/getPools/all/ethereum")
    assert urlopen.requests[0].get_header("User-agent") == http.USER_AGENT


async def test_the_desktop_post_names_itself_too(urlopen) -> None:
    await http.post_json("https://eth.example", {"method": "eth_call"})
    request = urlopen.requests[0]
    assert request.get_header("User-agent") == http.USER_AGENT
    assert request.get_header("Content-type") == "application/json"
