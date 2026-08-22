"""Handing a generated file to whoever asked for it, on either platform.

The two are genuinely different and neither is hard: a browser takes a blob
and a click on an anchor it never sees, a desktop build writes a file and has
to say *where*, because nothing pops up to tell them.

Flet-free apart from the page it is handed, so the caller can say what
happened in its own status line rather than this one guessing.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Where a desktop build puts a file, in the order they are tried.  The XDG
#: variable first because someone who has set it means it.
DOWNLOADS = ("XDG_DOWNLOAD_DIR",)


def is_browser() -> bool:
    return sys.platform == "emscripten"


def safe_name(name: str) -> str:
    """A filename that will survive both platforms.

    Slashes and colons are the ones that matter -- a pair is written "A/B"
    everywhere else in this app, and that is a directory separator here.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "download"


def save_text(name: str, text: str, *, media: str = "text/plain") -> str:
    """Write `text` out as `name`, and say where it went.

    In a browser that is the download the viewer chose, so there is nowhere
    to name and the message says so.
    """
    name = safe_name(name)
    if is_browser():
        _browser_download(name, text, media)
        return name
    path = _folder() / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _folder() -> Path:
    for variable in DOWNLOADS:
        named = os.environ.get(variable)
        if named and Path(named).is_dir():
            return Path(named)
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def _browser_download(name: str, text: str, media: str) -> None:
    """A blob, an anchor, and a click nobody sees.

    Revoked straight after: the object URL pins the blob in memory for the
    life of the document otherwise, and a route diagram is not small.
    """
    import js
    from pyodide.ffi import to_js

    options = to_js({"type": media}, dict_converter=js.Object.fromEntries)
    blob = js.Blob.new([text], options)
    url = js.URL.createObjectURL(blob)
    anchor = js.document.createElement("a")
    anchor.href = url
    anchor.download = name
    js.document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    js.URL.revokeObjectURL(url)


__all__ = ["is_browser", "safe_name", "save_text"]
