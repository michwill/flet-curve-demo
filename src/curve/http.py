"""Fetching JSON, on whatever platform this app happens to be running on.

The same split the wallet package uses, for the same reason: Flet runs this
Python either as CPython on a desktop or as Pyodide inside a browser Web
Worker, and those two have no HTTP client in common.

  * desktop -- `urllib.request`, stdlib, in a thread so the event loop keeps
    running while the socket blocks.
  * browser -- `pyodide.http.pyfetch`, which is `fetch()` and is available
    in a Worker (unlike `window`, which is why the wallet needs a bridge but
    this does not).

Both Curve APIs send `access-control-allow-origin: *`, so the browser half
needs no proxy and no CORS workaround -- verified against both hosts.

**The browser half sends no headers it does not have to**, which is not
tidiness. A request carrying only CORS-safelisted headers is a "simple"
one and goes straight out; add any header outside that list and the
browser must first ask the host's permission with an `OPTIONS` preflight,
which the host is then free to refuse. `User-Agent` is exactly such a
header -- see `USER_AGENT` for the one that cost a day.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlencode

#: Curve's edge returns 403 to the default `Python-urllib/x.y` agent. Only
#: that literal string is blocked, but a stdlib fallback would hit it, so
#: every request the *desktop* build makes carries a name of its own.
#:
#: **The browser build must not send it**, and this is worth spelling out
#: because sending it looked harmless for months. Setting `User-Agent` on
#: a `fetch` is legal per the Fetch standard -- it was taken off the
#: forbidden-header list -- so a browser that follows the standard puts it
#: on the wire. That makes the request non-simple, so a cross-origin GET
#: that used to go straight out now needs an `OPTIONS` preflight, and a
#: host that answers the preflight without naming `user-agent` in
#: `access-control-allow-headers` fails it. `chainlist.org` is such a host:
#: it answers `OPTIONS` with `access-control-allow-origin: *` and no
#: allow-headers at all.
#:
#: Chrome hid this. Chrome still strips `User-Agent` from `fetch` -- the
#: old behaviour -- so the header never reached the wire and the request
#: stayed simple. Every WebKit browser, which on iOS is *all* of them, and
#: Firefox, send it. So the endpoint directory failed to load on a phone
#: and nowhere else, and with no endpoints every read of every chain
#: failed for the rest of the session: pool parameters came up empty on
#: iOS while Chrome on a desktop was fine.
#:
#: Nothing is lost by dropping it. A browser sends its own `User-Agent`
#: and will not let a page forge one anyway; the header only ever existed
#: for `urllib`.
USER_AGENT = "flet-curve/0.1"

#: Cloudflare serves these with `s-maxage=300`; polling faster just returns
#: the same cached bytes.
DEFAULT_TIMEOUT = 30.0


class ApiError(Exception):
    """A request failed, or the API answered with something unusable.

    One exception type for the whole client so a UI needs one `except`, and
    every message is a sentence fit to show a user.
    """


def is_browser() -> bool:
    return sys.platform == "emscripten"


def build_url(base: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    if params:
        # Drop None so callers can pass optional query args unconditionally.
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            # `doseq` so a list value becomes the same key repeated, which
            # is how Merkl's `/v4/tokens?id=…&id=…` takes a batch. Without
            # it the list is stringified into one unusable `['a', 'b']`.
            url = f"{url}?{urlencode(clean, doseq=True)}"
    return url


async def get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET a URL and parse the response as JSON. Raises `ApiError`."""
    if is_browser():
        return await _get_json_browser(url, timeout)
    return await _get_json_desktop(url, timeout)


async def post_json(url: str, payload: Any, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST JSON and parse the answer. Raises `ApiError`.

    JSON-RPC needs this; the Curve APIs do not, which is why everything
    else here is a GET. Same platform split for the same reason.
    """
    body = json.dumps(payload)
    if is_browser():
        return await _post_json_browser(url, body, timeout)
    return await asyncio.to_thread(_post_blocking, url, body, timeout)


async def _post_json_browser(url: str, body: str, timeout: float) -> Any:
    from pyodide.http import pyfetch

    try:
        response = await asyncio.wait_for(
            pyfetch(
                url,
                method="POST",
                body=body,
                # `Content-Type` and nothing else. JSON-RPC needs this one
                # and it already costs a preflight -- every endpoint that
                # serves browsers answers it -- but `User-Agent` on top
                # would ask permission for a header many of them do not
                # name, and be refused. See `USER_AGENT`.
                headers={"Content-Type": "application/json"},
            ),
            timeout,
        )
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    except Exception as exc:
        raise ApiError(f"Network error: {exc}") from exc

    if response.status >= 400:
        raise ApiError(f"HTTP {response.status} from {url}")
    try:
        return await response.json()
    except Exception as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc


def _post_blocking(url: str, body: str, timeout: float) -> Any:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc


async def _get_json_browser(url: str, timeout: float) -> Any:
    from pyodide.http import pyfetch

    try:
        # No headers at all, so this stays a "simple" cross-origin request
        # and needs no preflight. See `USER_AGENT`.
        response = await asyncio.wait_for(pyfetch(url), timeout)
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    except Exception as exc:  # pyfetch raises bare JsException on network fail
        raise ApiError(f"Network error: {exc}") from exc

    if response.status >= 400:
        raise ApiError(f"HTTP {response.status} from {url}")
    try:
        return await response.json()
    except Exception as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc


async def _get_json_desktop(url: str, timeout: float) -> Any:
    # urllib blocks, so it goes to a worker thread; otherwise a slow API call
    # would freeze the whole UI, which on desktop is the same event loop.
    return await asyncio.to_thread(_fetch_blocking, url, timeout)


def _fetch_blocking(url: str, timeout: float) -> Any:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc
