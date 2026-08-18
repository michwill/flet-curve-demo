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

import asyncio
import json
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
#:
#: **And one size cannot serve this alone**, which is what took three
#: attempts to see. A mark is drawn between 14 and 38 logical pixels and
#: a screen puts between 1 and 4 device pixels on each, so the art has to
#: cover 14 through 152 -- an elevenfold spread. A single ceiling sized
#: for the top of it hands the renderer a sixfold reduction for the
#: ordinary case: a 24px mark on a 1x desktop is 27 device pixels, drawn
#: from 160.
#:
#: That is the one reduction CanvasKit does badly. It builds no mipmap
#: chain, so `FilterQuality.MEDIUM` has nothing to average over and comes
#: down to four bilinear taps spanning six source pixels. Measured against
#: curve.finance side by side -- whose marks are `<img>` elements put
#: through the browser's own downscaler, and are visibly crisper -- that
#: is the whole difference. The art was never the problem; the ratio it
#: was asked to survive was.
#:
#: So every mark is compiled at each of these, and the nearest size *up*
#: is what gets asked for. The renderer is then left a scale of at most
#: 2:1, which is the one reduction a four-tap bilinear does exactly: four
#: pixels averaged into one is a box filter, the same answer a mipmap
#: level would have held.
#:
#: **They double for that reason.** The worst case between two tiers is
#: their ratio, so a ratio of 2 is what bounds it at 2. Three tiers of
#: 32/64/160 was the first attempt and does not: 22px on a 3x screen is
#: 66 device pixels, one past 64, and falls all the way to 160 -- a 2.4x
#: reduction, back in the territory this exists to leave. The test that
#: sweeps every drawn size against every ratio a screen reports is what
#: caught it, which is the test that was missing all along.
#:
#: 20 at the bottom because the smallest mark is 14px and a 1x screen
#: draws it at 14; 160 at the top because the largest is 38px and a 4x
#: screen draws it at 152.
MARK_TIERS = (20, 40, 80, 160)

#: The largest tier, and so the size nothing is compiled above. Named
#: separately because it is the ceiling the build enforces, whereas the
#: tuple above is the set of sizes it writes.
MARK_PIXELS = MARK_TIERS[-1]


def mark_tier(device_pixels: float) -> int:
    """The smallest compiled size that still covers what will be drawn.

    Rounds *up*, deliberately. Art below the size it is drawn at is
    magnified, and magnification is the one direction no filter recovers
    from -- it invents nothing and smears what is there. Art above it is
    only a reduction, and by construction a small enough one to be done
    well.

    Past the largest tier there is no more art, so that is the answer for
    anything bigger.
    """
    for tier in MARK_TIERS:
        if device_pixels <= tier:
            return tier
    return MARK_TIERS[-1]


def tiered(filename: str, tier: int) -> str:
    """`0xabc.png` at tier 64 is `0xabc@64.png`.

    The suffix goes before the extension rather than into a per-tier
    directory so that one token's sizes sort together, and so a missing
    tier is visible beside the ones that were written.
    """
    stem, dot, suffix = filename.rpartition(".")
    return f"{stem}@{tier}{dot}{suffix}" if dot else f"{filename}@{tier}"

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


#: One chain's marks at one tier, once fetched: `(chain, tier)` -> bytes
#: per address. Empty until something calls `remember_bundle`, and a miss
#: is not an error -- `token_logo` falls back to the file's own URL, which
#: is what every build did before bundles existed and what desktop still
#: does.
_BUNDLES: dict[tuple[str, int], dict[str, bytes]] = {}


def bundle_url(chain: str, tier: int, suffix: str) -> str:
    """Where one chain's bundle lives. See `BUNDLE_STEM` in build_assets."""
    return asset_url("tokens", chain, f"marks@{tier}{suffix}")


def remember_bundle(chain: str, tier: int, blob: bytes, index: dict) -> int:
    """Cut a fetched bundle into one PNG per address. Returns how many.

    The slices are the original files byte for byte -- the bundle is a
    concatenation, not a container -- so nothing is decoded here and no
    image library is needed in Pyodide.

    A truncated or mismatched entry is dropped rather than stored: a short
    slice is not a PNG, and `ft.Image` would render nothing where the URL
    fallback would have rendered a logo.
    """
    marks = {}
    for address, span in (index or {}).items():
        try:
            start, length = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        chunk = blob[start : start + length]
        if len(chunk) == length and chunk[:4] == b"\x89PNG":
            marks[str(address).lower()] = chunk
    if marks:
        _BUNDLES[(chain, tier)] = marks
    return len(marks)


def forget_bundles() -> None:
    """Drop every cached bundle. For tests, and for a chain switch that
    wants the memory back."""
    _BUNDLES.clear()


async def load_bundle(chain: str, device_pixels: float, fetch) -> int:
    """Fetch one chain's mark bundle and remember it. Returns how many.

    `fetch(url)` returns the bytes at a URL, or raises. Injected rather
    than imported so this is testable without a network and so the browser
    and desktop transports stay where they already live.

    **Nothing here is allowed to break a page.** A build with no bundles,
    a gateway that will not serve one, a truncated index: all of them come
    back as zero, and every mark then asks for its own file exactly as it
    did before bundles existed. That is the whole safety argument for
    putting this in front of a working path.
    """
    tier = mark_tier(device_pixels)
    if (chain, tier) in _BUNDLES:
        return len(_BUNDLES[(chain, tier)])
    try:
        blob = await fetch(bundle_url(chain, tier, ".bin"))
        raw = await fetch(bundle_url(chain, tier, ".json"))
        index = json.loads(bytes(raw))
    except asyncio.CancelledError:
        # The page gave up on this chain. Not a failure of the bundle, and
        # swallowing it would leave the task looking like it succeeded.
        raise
    except Exception:
        return 0
    return remember_bundle(chain, tier, bytes(blob), index)


def bundled_mark(chain: str, address: str, device_pixels: float) -> bytes | None:
    """One mark's PNG out of a fetched bundle, or None if it is not there.

    None covers both "no bundle for this chain" and "this token is not in
    it", which the caller treats the same way: ask for the file.
    """
    if not chain or not address:
        return None
    marks = _BUNDLES.get((chain, mark_tier(device_pixels)))
    return marks.get(address.strip().lower()) if marks else None


def chain_logo(chain: str, device_pixels: float = MARK_PIXELS) -> str | None:
    """The network's mark, for the chain picker.

    `device_pixels` is how large it will actually be drawn, in device
    pixels -- the logical size times the screen's ratio. It picks the
    compiled tier; the default asks for the largest, which is what a
    caller that does not know the ratio should get.
    """
    name = (chain or "").strip().lower()
    if not name:
        return None
    filename = tiered(f"{name}.png", mark_tier(device_pixels))
    relative = f"chains/{filename}"
    return asset_url("chains", filename) if _exists(relative) else None


def token_logo(
    chain: str, address: str, device_pixels: float = MARK_PIXELS
) -> str | None:
    """A token's mark. Upstream names them by lowercased address."""
    if not chain or not address:
        return None
    filename = tiered(f"{address.strip().lower()}.png", mark_tier(device_pixels))
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
