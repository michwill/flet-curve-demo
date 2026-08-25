"""Fetching JSON, on whatever platform this app happens to be running on."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from typing import Any
from urllib.parse import urlencode, urlsplit

#: Curve's edge returns 403 to the default `Python-urllib/x.y` agent.
USER_AGENT = "flet-curve/0.1"

#: Cloudflare serves these with `s-maxage=300`; polling faster just returns
#: the same cached bytes.
DEFAULT_TIMEOUT = 30.0

#: How many kept-alive connections to hold per host.  The desktop build reads
#: through a small pool of worker threads and one connection each is enough;
#: what matters is that a *second* request to a host it has already spoken to
#: does not pay for the handshake again.
#:
#: Measured against the Curve API, six sequential page reads: 7.85s opening a
#: connection each time against 1.05s over one kept open, and the spread went
#: from 140ms-5,356ms to a steady 88-114ms after the first.  Most of what
#: looks like a slow API is the setup, and most of the variance is too.
POOL_PER_HOST = 2

#: How many redirects to follow.  `urlopen` did this; `http.client` does not,
#: so it is done here rather than lost.
MAX_REDIRECTS = 5

#: Only ever the browser's job otherwise: `js.fetch` pools connections itself,
#: so none of this is reached there.
_POOL: dict[tuple[str, str, int], list[Any]] = {}
_POOL_LOCK = threading.Lock()


def _key(parts) -> tuple[str, str, int]:
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname or "", port


def _take(parts, timeout: float):
    """A connection to that host: one that is already open, or a new one."""
    import http.client

    key = _key(parts)
    with _POOL_LOCK:
        waiting = _POOL.get(key)
        if waiting:
            found = waiting.pop()
            found.timeout = timeout
            return found
    if parts.scheme == "https":
        import ssl

        return http.client.HTTPSConnection(
            key[1], key[2], timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(key[1], key[2], timeout=timeout)


def _give_back(parts, connection) -> None:
    """Keep it for the next caller, unless the shelf is full."""
    key = _key(parts)
    with _POOL_LOCK:
        waiting = _POOL.setdefault(key, [])
        if len(waiting) < POOL_PER_HOST:
            waiting.append(connection)
            return
    connection.close()


def forget_connections() -> None:
    """Drop every kept connection. For tests, and for a network that moved."""
    with _POOL_LOCK:
        held = [c for waiting in _POOL.values() for c in waiting]
        _POOL.clear()
    for connection in held:
        with contextlib.suppress(Exception):
            connection.close()


class ApiError(Exception):
    """A request failed, or the API answered with something unusable.

    `status` is the HTTP code where there was one, and None where the request
    never got an answer at all.  The two are different questions: a 404 says
    the thing is not there and asking again will not help, where a dropped
    connection says nothing about whether it exists.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_browser() -> bool:
    return sys.platform == "emscripten"


def build_url(base: str, path: str, params: dict[str, Any] | None = None) -> str:
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
    return url


async def get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """GET a URL and parse the response as JSON. Raises `ApiError`."""
    if is_browser():
        return await _get_json_browser(url, timeout)
    return await _get_json_desktop(url, timeout)


async def post_json(url: str, payload: Any, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST JSON and parse the answer. Raises `ApiError`."""
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
                headers={"Content-Type": "application/json"},
            ),
            timeout,
        )
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    except Exception as exc:
        raise ApiError(f"Network error: {exc}") from exc

    if response.status >= 400:
        raise ApiError(f"HTTP {response.status} from {url}",
                       status=response.status)
    try:
        return await response.json()
    except Exception as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc


def _post_blocking(url: str, body: str, timeout: float) -> Any:
    status, payload, place = _request(
        url, timeout, body=body.encode(), content_type="application/json")
    if status >= 400:
        raise ApiError(f"HTTP {status} from {place}", status=status)
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise ApiError(f"Response was not valid JSON: {place}") from exc


async def get_bytes(url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Raw bytes at a URL. For files that are not JSON and not images."""
    if is_browser():
        try:
            return await asyncio.wait_for(_browser_bytes(url), timeout)
        except TimeoutError:
            raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    return await asyncio.to_thread(_read_blocking, url, timeout)


async def _browser_bytes(url: str) -> bytes:
    """The fetch itself. Separated so the deadline above can be tested.

    It needs one: a stalled connection here used to block the pool list
    indefinitely, because `CurveApp._load_marks` fetches the mark bundles
    through this and `load_pools` awaits it before the first page of rows.
    A gateway that answers 504 raises; one that accepts and never replies
    did not.
    """
    from js import fetch as js_fetch

    response = await js_fetch(url)
    if not response.ok:
        raise ApiError(f"HTTP {response.status} from {url}",
                       status=response.status)
    buffer = await response.arrayBuffer()
    return bytes(buffer.to_py())


def _read_blocking(url: str, timeout: float) -> bytes:
    status, payload, place = _request(url, timeout)
    if status >= 400:
        # `status` carried, because `ui.assets` reads it: a 404 on the second
        # half of a mark bundle says there was never a second half, and a 504
        # says the gateway has not found the blocks yet.
        raise ApiError(f"could not read {place}: HTTP {status}", status=status)
    return payload


def _request(url: str, timeout: float, *, body: bytes | None = None,
             content_type: str = "") -> tuple[int, bytes, str]:
    """One request over a kept-alive connection, following redirects.

    Returns `(status, payload, url)` -- the last because a redirect changes
    where the answer came from, and the caller's error messages should name
    the place that actually answered.

    A pooled connection can be closed at the far end between uses and nothing
    says so until the write fails, so a request that dies on a *reused*
    connection is retried once on a fresh one.  That is the one retry: a
    second failure is the host, not the socket.
    """
    import http.client

    seen = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(seen)
        if parts.scheme not in ("http", "https"):
            raise ApiError(f"Not a fetchable URL: {seen}")
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        headers = {"User-Agent": USER_AGENT, "Connection": "keep-alive"}
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in (0, 1):
            connection = _take(parts, timeout)
            # A pooled socket is one somebody has already spoken over; a fresh
            # one has none yet.  Only the first kind is worth a second go.
            reused = connection.sock is not None
            try:
                connection.request("GET" if body is None else "POST", target,
                                   body=body, headers=headers)
                answer = connection.getresponse()
                payload = answer.read()
                status, place = answer.status, answer.getheader("Location") or ""
            except (http.client.HTTPException, OSError) as exc:
                with contextlib.suppress(Exception):
                    connection.close()
                if attempt == 0 and reused:
                    continue        # the far end had hung up; try a new socket
                if isinstance(exc, TimeoutError):
                    raise ApiError(
                        f"Timed out after {timeout:.0f}s: {seen}") from None
                raise ApiError(f"Could not reach {seen}: {exc}") from exc
            if answer.will_close:
                connection.close()
            else:
                _give_back(parts, connection)
            break
        if status in (301, 302, 303, 307, 308) and place:
            from urllib.parse import urljoin

            seen = urljoin(seen, place)
            if status == 303:
                body, content_type = None, ""
            continue
        return status, payload, seen
    raise ApiError(f"Too many redirects from {url}")


async def _get_json_browser(url: str, timeout: float) -> Any:
    from pyodide.http import pyfetch

    try:
        response = await asyncio.wait_for(pyfetch(url), timeout)
    except TimeoutError:
        raise ApiError(f"Timed out after {timeout:.0f}s: {url}") from None
    except Exception as exc:  # pyfetch raises bare JsException on network fail
        raise ApiError(f"Network error: {exc}") from exc

    if response.status >= 400:
        raise ApiError(f"HTTP {response.status} from {url}",
                       status=response.status)
    try:
        return await response.json()
    except Exception as exc:
        raise ApiError(f"Response was not valid JSON: {url}") from exc


async def _get_json_desktop(url: str, timeout: float) -> Any:
    # urllib blocks, so it goes to a worker thread; otherwise a slow API
    # call would freeze the whole UI, which on desktop is the same event
    # loop.
    return await asyncio.to_thread(_fetch_blocking, url, timeout)


def _fetch_blocking(url: str, timeout: float) -> Any:
    status, payload, place = _request(url, timeout)
    if status >= 400:
        raise ApiError(f"HTTP {status} from {place}", status=status)
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ApiError(f"Response was not valid JSON: {place}") from exc
