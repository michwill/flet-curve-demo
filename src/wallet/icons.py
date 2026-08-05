"""Icons the app ships for connectors that announce none.

These live next to the Python code (`wallet/icons/`) rather than in Flet's
assets directory, and are handed to the UI as `data:` URIs. Both of those
choices are forced, for the same underlying reason -- the browser build and
the desktop build resolve things differently:

  * `flet publish` **excludes the assets directory** from the archive
    Pyodide unpacks, so a file under `src/assets/` is not on the Python
    filesystem in the browser at all. Under `src/wallet/` it is.
  * Flutter web resolves a *relative* `Image.src` against its own asset
    bundle rather than the site root, so `src="walletconnect.svg"` renders
    on desktop and silently draws nothing on web -- no load error, so not
    even `error_content` fires. (Verified both ways.)

Reading the bytes here and emitting a `data:` URI removes the difference:
one code path, byte-identical on every platform, and no network fetch.
"""

from __future__ import annotations

from base64 import b64encode
from functools import lru_cache
from pathlib import Path

_ICON_DIR = Path(__file__).parent / "icons"

_MIME = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

#: Connector id (as reported by the bridge) -> bundled icon filename.
#: Injected wallets are not listed: EIP-6963 requires them to announce their
#: own icon, and using ours instead would be wrong as well as unnecessary.
_BY_CONNECTOR = {
    "walletconnect": "walletconnect.svg",
}


@lru_cache(maxsize=None)
def data_uri(filename: str) -> str | None:
    """Load a bundled icon as a `data:` URI, or None if it is missing.

    Missing is not fatal: the UI falls back to a lettered tile, and an
    example app should not refuse to start over a decoration.
    """
    path = _ICON_DIR / filename
    mime = _MIME.get(path.suffix.lower())
    if mime is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64,{b64encode(raw).decode('ascii')}"


def for_connector(connector: str | None) -> str | None:
    """The icon this app ships for a connector, if any."""
    filename = _BY_CONNECTOR.get(connector or "")
    return data_uri(filename) if filename else None
