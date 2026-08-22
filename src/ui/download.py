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


#: The channel `download_bridge.js` listens on, and the version of what is
#: sent over it.  Both halves have to agree; the JS is the other side.
CHANNEL = "flet-download"
VERSION = 1


def save_text(name: str, text: str, *, media: str = "text/plain",
              page=None) -> str:
    """Write `text` out as `name`, and say where it went.

    A browser downloads it wherever downloads go, so there is nowhere to
    name and the empty string says so.
    """
    name = safe_name(name)
    if is_browser():
        _browser_download(name, text, media)
        return ""
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
    """Hand the file to the main thread, which has a DOM to save it with.

    Not `<a download>` here, the obvious way, because it cannot be done here:
    Flet runs this in a module Web Worker and a worker has no `document`,
    which is exactly what it said when it was tried.  Two things that looked
    like ways round it are not: a blob URL made in the worker is valid on
    this origin but Flet's `launch_url` will not open one, and it will not
    open a `data:` URL either -- Flutter's launcher takes http-ish schemes
    and drops the rest without a word.

    So it goes over a `BroadcastChannel` to `download_bridge.js`, which is
    how the wallet already crosses the same gap.
    """
    import js
    from pyodide.ffi import to_js

    message = to_js(
        {"v": VERSION, "dir": "save", "name": name, "text": text, "media": media},
        dict_converter=js.Object.fromEntries,
    )
    channel = js.BroadcastChannel.new(CHANNEL)
    try:
        channel.postMessage(message)
    finally:
        channel.close()


__all__ = ["CHANNEL", "VERSION", "is_browser", "safe_name", "save_text"]
