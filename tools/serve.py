#!/usr/bin/env python3
"""Serve `dist/` without caching anything.

    python tools/serve.py            # http://127.0.0.1:8000
    python tools/serve.py 8022 dist

`python -m http.server` sends no cache headers at all, which Chrome reads
as "cache it heuristically". For a normal static site that is fine; for a
Flet build it means a reload can quietly keep serving the *previous*
`app.tar.gz` and `wallet_bridge.js` -- so a fix that is already published
appears not to work, and the only clue is that the browser and the file on
disk disagree.

That cost real time twice: a wallet fix that was live in the build but not
in the running page. So this refuses to cache, and says which build it is
serving on every start.
"""

from __future__ import annotations

import http.server
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        """Serve `index.html` for any path that is not a file.

        The published build routes on the URL *path* (`/ethereum/0xC09e…`
        is a pool page), which a static server knows nothing about: it
        looks for a directory of that name and 404s. Every single-page app
        needs this fallback, and without it a shared link only works if
        you were already in the app.

        Real files are still served as files, so this cannot shadow the
        assets -- only paths that do not exist reach the app.
        """
        path = Path(self.translate_path(self.path))
        if not path.exists() and "." not in path.name:
            self.path = "/index.html"
        return super().send_head()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        # One line per request is noise; failures are not.
        if not str(args[1] if len(args) > 1 else "").startswith("2"):
            super().log_message(format, *args)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    directory = ROOT / (sys.argv[2] if len(sys.argv) > 2 else "dist")

    archive = directory / "app.tar.gz"
    if not archive.is_file():
        print(f"{directory} has no app.tar.gz -- run `flet publish` first.")
        return 1

    stamp = archive.stat().st_mtime
    from datetime import datetime

    print(f"serving {directory.relative_to(ROOT)} (built {datetime.fromtimestamp(stamp):%H:%M:%S})")
    print(f"  http://127.0.0.1:{port}/          no-store, so a reload is always the current build")
    print(f"  http://127.0.0.1:{port}/?mock=1   with the fake wallet")

    handler = lambda *a, **kw: NoCacheHandler(*a, directory=str(directory), **kw)  # noqa: E731
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
