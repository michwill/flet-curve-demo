#!/usr/bin/env python3
"""Render the app icon and favicon from the Curve mark.

The source is `src/assets/curve/branding/logo.svg`, which comes out of the
curve-assets submodule via `build_assets.py`. That directory is generated
and gitignored; the PNGs this writes are **committed**, because a site
needs a favicon whether or not whoever cloned it has initialised a
submodule, and because `flet build` cannot read an SVG at all.

    python tools/build_icons.py

Four kinds of output, and they differ for reasons worth stating:

  * `favicon.png` -- 32px, what a browser tab shows. Transparent: tab bars
    are light or dark depending on the theme and the mark reads on both.
  * `icons/icon-{192,512}.png` -- the PWA's own icons, same treatment.
  * `icons/icon-maskable-*.png` and `apple-touch-icon-192.png` -- these get
    *cropped* to whatever shape the platform likes (a circle, a squircle)
    and iOS composites transparency onto black. So they are drawn on an
    opaque background, inset to Android's safe zone: a maskable icon may
    lose everything outside the middle 80%.
  * `icon.png` -- 1024px, the one `flet build` slices up for desktop and
    mobile packages;
  * `icons/loading-animation.png` -- what the page shows while the Python
    runtime starts, in place of Flet's own logo.

Rendering goes through `rsvg-convert` at each output size rather than
downscaling one big raster: the mark is a mesh gradient, and letting the
renderer antialias at the target size keeps the 32px version legible.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "src" / "assets" / "curve" / "branding" / "logo.svg"
ASSETS = ROOT / "src" / "assets"

#: Android crops a maskable icon to an arbitrary shape and guarantees only
#: the middle 80% survives. 0.78 keeps a little margin on top of that.
SAFE_ZONE = 0.78

#: What the cropped icons sit on. White rather than the page background,
#: because these show up on a home screen next to other apps rather than
#: inside the app.
BACKDROP = (255, 255, 255, 255)


def render(size: int) -> "Image.Image":  # noqa: F821
    """The mark at `size`, transparent, antialiased by the SVG renderer."""
    from PIL import Image  # noqa: PLC0415

    out = subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), str(LOGO)],
        check=True,
        capture_output=True,
    ).stdout
    import io  # noqa: PLC0415

    return Image.open(io.BytesIO(out)).convert("RGBA")


def on_backdrop(size: int) -> "Image.Image":  # noqa: F821
    """The mark inset into the safe zone, on an opaque square."""
    from PIL import Image  # noqa: PLC0415

    inner = round(size * SAFE_ZONE)
    canvas = Image.new("RGBA", (size, size), BACKDROP)
    mark = render(inner)
    offset = (size - inner) // 2
    canvas.alpha_composite(mark, (offset, offset))
    return canvas


def write(image: "Image.Image", path: Path) -> None:  # noqa: F821
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    print(f"  {path.relative_to(ROOT)}  {image.size[0]}px  {path.stat().st_size:,} B")


def main() -> int:
    if not LOGO.exists():
        print(
            f"No logo at {LOGO.relative_to(ROOT)}.\n"
            "Run `git submodule update --init` then `python tools/build_assets.py`.",
            file=sys.stderr,
        )
        return 1
    if shutil.which("rsvg-convert") is None:
        print("rsvg-convert not found (librsvg).", file=sys.stderr)
        return 1
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        print("Pillow not installed.", file=sys.stderr)
        return 1

    print("Rendering the Curve mark:")
    write(render(32), ASSETS / "favicon.png")
    write_ico(ASSETS / "favicon.ico")
    write(render(1024), ASSETS / "icon.png")
    for size in (192, 512):
        write(render(size), ASSETS / "icons" / f"icon-{size}.png")
        write(on_backdrop(size), ASSETS / "icons" / f"icon-maskable-{size}.png")
    write(on_backdrop(192), ASSETS / "icons" / "apple-touch-icon-192.png")
    # The image the page shows while Pyodide starts. Flet ships its own
    # logo there; this is the same override as the favicon -- same name,
    # copied over theirs at publish time. 512px because the loading
    # screen's CSS scales it down to ~40% and then zooms it away, so it is
    # briefly large on a high-density screen.
    write(render(512), ASSETS / "icons" / "loading-animation.png")
    write_x11_icon(ASSETS / "window_icon.argb")
    print("These overwrite Flet's defaults on `flet publish` and `flet build`.")
    return 0


def write_ico(path: Path) -> None:
    """A real `favicon.ico`, for the path browsers probe on their own.

    The link tag is what a modern browser uses, but every one of them
    still asks for `/favicon.ico` first, and answering that with a 404 is
    the sort of thing that looks like a broken build in a network log.
    """
    icon = render(48)
    icon.save(path, "ICO", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"  {path.relative_to(ROOT)}  16/32/48px  {path.stat().st_size:,} B")


#: Sizes for the X11 window icon. A window manager picks whichever is
#: closest to what it is drawing -- a titlebar wants 24, a task switcher
#: 48 or 64 -- and having a small one rendered rather than downscaled is
#: the difference between a legible titlebar and a smear.
X11_SIZES = (16, 24, 32, 48, 64, 128)


def write_x11_icon(path: Path) -> None:
    """The window icon, pre-decoded into what `_NET_WM_ICON` wants.

    That property is a list of `width, height, then width*height pixels`
    in 0xAARRGGBB, repeated once per size. Writing it here rather than
    decoding a PNG at runtime keeps the app itself free of any image
    library: `ui.window_icon` reads this with `struct` and nothing else.

    Little-endian `uint32` on disk. The X11 side wants C `long`s, which
    are eight bytes on 64-bit, so the reader widens them -- doing that
    here instead would make the file architecture-specific.
    """
    import struct  # noqa: PLC0415

    words: list[int] = []
    for size in X11_SIZES:
        image = render(size)
        words += [size, size]
        raw = image.tobytes()  # RGBA, row-major, no padding
        for offset in range(0, len(raw), 4):
            r, g, b, a = raw[offset : offset + 4]
            words.append((a << 24) | (r << 16) | (g << 8) | b)
    path.write_bytes(struct.pack(f"<{len(words)}I", *words))
    print(f"  {path.relative_to(ROOT)}  {'/'.join(map(str, X11_SIZES))}px  "
          f"{path.stat().st_size:,} B")


if __name__ == "__main__":
    raise SystemExit(main())
