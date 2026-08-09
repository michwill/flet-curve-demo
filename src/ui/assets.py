"""Finding the compiled curve-assets images, on either platform.

`tools/build_assets.py` copies the subset this app needs out of the
curve-assets submodule into `src/assets/curve/`. This module turns a chain
or a token address into something `ft.Image` will actually load, which is
not the same string on both platforms:

> Flutter resolves a *relative* `Image.src` against its own asset bundle.
> That is exactly right on desktop, where `flet run` serves `src/assets`,
> and draws **nothing** on web, where the bundle is not where the files
> went. On web `flet publish` copies `src/assets/**` to the site root, so
> the same file is reachable at an absolute URL path.

flet-pay-example hit the same wall with one icon and solved it by inlining
a data URI. That does not scale to 900 token logos, so this dispatches on
platform instead: an absolute path in the browser, a relative one on the
desktop.

Nothing here raises. A token with no logo upstream is the normal case, not
an error, so callers get `None` and draw initials instead.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

#: Where `build_assets.py` writes, relative to the Flet assets directory.
ASSET_ROOT = "curve"

#: How large a compiled mark is, in pixels, on the long side.
#:
#: Upstream art is 200 to 280px and every mark here is drawn between 14
#: and 38, so the renderer was being handed a tenfold reduction and asked
#: to make it look good. It cannot: minification wants an average over
#: every contributing pixel, which is what a mipmap is, and CanvasKit does
#: not build one -- so in a browser every filter quality produced the same
#: aliased result, worst on the smallest mark on the page.
#:
#: A resampler that *does* average over everything is easy to run once, at
#: build time, where nothing is in a hurry. So the art arrives close to
#: the size it is drawn at and the runtime step is small enough that any
#: filter can do it.
#:
#: 160 because the largest mark drawn is 38px -- the pool stack on a
#: detail page -- and the densest screens put four device pixels on each
#: logical one: 152, rounded up. 128 was the first answer and covers a
#: ratio of 3 but not 3.5, which real phones report; the test that asks
#: for every ratio up to 4 is what caught it, and 25% more art is a
#: cheaper mistake than a mark being upscaled on the newest handsets.
#:
#: A *ceiling*: art that arrives smaller is left alone rather than blown
#: up to meet it.
MARK_PIXELS = 160

#: The same directory on the Python filesystem, for existence checks. On
#: web this path does not exist -- `flet publish` deliberately leaves the
#: assets out of the archive Pyodide unpacks -- so checks are skipped there
#: and a missing image simply fails to paint.
_LOCAL_ROOT = Path(__file__).resolve().parent.parent / "assets" / ASSET_ROOT


def is_browser() -> bool:
    return sys.platform == "emscripten"


@lru_cache(maxsize=1)
def _web_base() -> str:
    """The URL the app was served from, for building absolute asset URLs.

    Flet treats an `Image.src` that does not look like a URL as a path into
    the Flutter *asset bundle*. On web the compiled assets are not in that
    bundle -- `flet publish` copies them to the site root instead -- so a
    relative path finds nothing and a leading slash is not enough either.
    They need a real URL.

    Taken from the worker's own location rather than a hardcoded origin, so
    an app served under a sub-path still resolves. There is no `window` in
    a Web Worker, but there is a `location`.
    """
    try:
        import js

        href = str(js.location.href)
    except Exception:
        return ""
    return href.rsplit("/", 1)[0] + "/"


def asset_url(*parts: str) -> str:
    """Something `ft.Image` can load, for a file under the assets root."""
    path = "/".join((ASSET_ROOT, *parts))
    if not is_browser():
        # Desktop resolves relative paths against `assets_dir`, which is
        # exactly where these live.
        return path
    return f"{_web_base()}{path}"


@lru_cache(maxsize=4096)
def _exists(relative: str) -> bool:
    """Is the file actually there? Always True in the browser.

    Pyodide cannot see the assets directory, so guessing "yes" is the only
    option there -- a wrong guess costs a 404 and an unpainted image, which
    is what a missing logo looks like anyway.
    """
    if is_browser():
        return True
    return (_LOCAL_ROOT / relative).is_file()


def chain_logo(chain: str) -> str | None:
    """The network's mark, for the chain picker."""
    name = (chain or "").strip().lower()
    if not name:
        return None
    relative = f"chains/{name}.png"
    return asset_url("chains", f"{name}.png") if _exists(relative) else None


def token_logo(chain: str, address: str) -> str | None:
    """A token's mark. Upstream names them by lowercased address."""
    if not chain or not address:
        return None
    filename = f"{address.strip().lower()}.png"
    relative = f"tokens/{chain}/{filename}"
    return asset_url("tokens", chain, filename) if _exists(relative) else None


def curve_logo() -> str | None:
    """The Curve mark, without the wordmark, for the header."""
    return asset_url("branding", "logo.svg") if _exists("branding/logo.svg") else None


def bundled(name: str) -> str:
    """A file committed straight into `src/assets`.

    The same web/desktop split as `asset_url`, without the `curve/` prefix:
    these are not compiled from the submodule, they are in the repository.
    """
    return name if not is_browser() else f"{_web_base()}{name}"


def chad_mark() -> str:
    """The Chad, for the theme button.

    From curve-frontend (`packages/ui/src/images/chad.png`) rather than
    curve-assets, which is why it is committed rather than compiled.
    """
    return bundled("chad.png")


#: Chains whose display name is not just a capitalisation of the API's.
CHAIN_NAMES = {
    "bsc": "BNB Chain",
    "xdai": "Gnosis",
    "zksync": "zkSync",
    "zkevm": "Polygon zkEVM",
    "x-layer": "X Layer",
    "arbitrum": "Arbitrum",
    "avalanche": "Avalanche",
    "fraxtal": "Fraxtal",
    "hyperliquid": "Hyperliquid",
}


def chain_name(chain: str) -> str:
    """"ethereum" -> "Ethereum", "bsc" -> "BNB Chain"."""
    if not chain:
        return ""
    known = CHAIN_NAMES.get(chain.lower())
    if known:
        return known
    # Hyphens and underscores are word breaks upstream, not punctuation.
    return " ".join(part.capitalize() for part in chain.replace("_", "-").split("-"))
