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
    mobile packages.

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
    write(render(1024), ASSETS / "icon.png")
    for size in (192, 512):
        write(render(size), ASSETS / "icons" / f"icon-{size}.png")
        write(on_backdrop(size), ASSETS / "icons" / f"icon-maskable-{size}.png")
    write(on_backdrop(192), ASSETS / "icons" / "apple-touch-icon-192.png")
    print("These overwrite Flet's defaults on `flet publish` and `flet build`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
