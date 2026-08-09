#!/usr/bin/env python3
"""Compile the subset of curve-assets this app needs into `src/assets`.

The upstream repo is 67 MB. Copying it wholesale would put the tests,
the SVG sources and the git history into every `flet publish` output, so
this takes only what the app can draw: the chain logos (388 KB for all 40),
the Curve mark, and the token images for **every chain upstream has** --
which is every chain the picker can offer, including the Curve Lite ones.

Run it after cloning, and again whenever the submodule is updated:

    git submodule update --init
    python tools/build_assets.py

The output is generated and gitignored. Everything that reads it degrades
to a lettered circle when a file is missing -- which is not only about this
build step being skipped: plenty of real tokens have no logo upstream.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "vendor" / "curve-assets"
TARGET = ROOT / "src" / "assets" / "curve"

#: Upstream puts Ethereum in `images/assets` and everything else in
#: `images/assets-<chain>`, where `<chain>` is the name Curve's API uses --
#: which is why Gnosis appears as `assets-xdai`.
def token_dir(chain: str) -> str:
    return "assets" if chain == "ethereum" else f"assets-{chain}"


def available_chains(source: Path) -> list[str]:
    """Every chain the submodule has token images for.

    Taken from the directory listing rather than a list kept here, because
    a list kept here goes stale silently: the app draws lettered initials
    for a token with no image, so a chain missing entirely looks exactly
    like a chain whose tokens upstream has not got round to. That is how
    Gnosis ended up with no logos at all -- it was never in the list, and
    nothing said so.

    The whole tree is ~32 MB across 38 chains, against ~21 MB for Ethereum
    and five others, so taking all of them costs about half again. On web
    these are separate files served from the site root, fetched only when
    a pool that uses one is on screen -- the page load does not carry them.
    """
    images = source / "images"
    if not images.is_dir():
        return []
    chains = ["ethereum"] if (images / "assets").is_dir() else []
    chains += sorted(
        item.name[len("assets-") :]
        for item in images.iterdir()
        if item.is_dir() and item.name.startswith("assets-")
    )
    return chains


#: How much breathing room a full-bleed logo gets, as a fraction of its
#: width. The app draws every token mark as a circle, and a circle cut out
#: of a square loses 21% of it -- corners *and* whatever artwork reaches
#: them. crvUSD is the obvious victim: its torus touches all four edges,
#: so the mark came out visibly clipped.
#:
#: Padding it with its own background colour means the circle eats
#: background instead of artwork. A full 41% would fit the entire square
#: inside the circle, and would leave these logos looking smaller than
#: everything beside them; a fifth is the compromise -- the artwork
#: survives, the mark still fills its circle.
BLEED_PAD = 0.20

#: The size marks are compiled down to. Defined next to the code that
#: reads them back, so the decoder cannot be told to ask for resolution
#: this step did not produce -- see `ui.assets.MARK_PIXELS`.
sys.path.insert(0, str(ROOT / "src"))
from ui.assets import MARK_PIXELS  # noqa: E402


def shrink(image, target: int = MARK_PIXELS):
    """`image` at `target` pixels on the long side, or unchanged if smaller.

    **Premultiplied**, which is the whole reason this is more than one
    call. A resampler averages each channel on its own, so it happily
    mixes the colour of pixels that are entirely transparent into the
    edge -- and in these files those pixels are white. Averaged with the
    rim of a coloured disc, that lightens it: a pale halo, worst where the
    reduction is largest, which is exactly how the network mark looked on
    a phone.

    Multiplying colour by alpha first means a transparent pixel
    contributes nothing at all, which is what "transparent" should mean.
    The division afterwards puts the colour back for the pixels that
    survived, leaving the ones that did not as they were -- their colour
    is unused, and dividing by zero to invent one would be worse than
    leaving it.

    `BOX` rather than `LANCZOS`, which is the filter you would reach for
    first and the wrong one here. Box *is* the area average -- every
    source pixel counted once, in proportion to how much of the output
    pixel it covers -- which is precisely the mipmap level this whole
    exercise is about not having. Lanczos sharpens, and sharpening a
    reduction means ringing: on one chain mark it invented 501 distinct
    colours where box produced 230, and the PNG came out at 9,755 bytes
    against 5,488. Worse looking and nearly twice the size.
    """
    import numpy as np
    from PIL import Image

    if max(image.size) <= target:
        return image

    rgba = np.asarray(image.convert("RGBA"), dtype=np.float64)
    alpha = rgba[..., 3] / 255.0
    premultiplied = rgba[..., :3] * alpha[..., None]

    width, height = image.size
    scale = target / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))

    # One channel at a time, in float. Never as an RGBA image: putting
    # premultiplied data through uint8 is what produced the white rim this
    # whole function exists to avoid. At alpha 16/255 the premultiplied
    # blue of this app's own network mark is 14.7, and rounding that to 15
    # is a 2% error on a number that is then multiplied by 1/alpha -- 16x --
    # so it lands 494 on a channel that stops at 255. Clipped: white.
    # Measured, on the mark that was reported: (239,239,255) against the
    # (98,126,234) it should have been.
    def resized(plane: np.ndarray) -> np.ndarray:
        return np.asarray(
            Image.fromarray(plane.astype(np.float32), "F").resize(
                size, Image.Resampling.BOX
            ),
            dtype=np.float64,
        )

    small_alpha = resized(rgba[..., 3])
    out = np.zeros((size[1], size[0], 4))
    out[..., 3] = small_alpha

    coverage = small_alpha / 255.0
    lit = coverage > 0
    for channel in range(3):
        out[..., channel][lit] = resized(premultiplied[..., channel])[lit] / coverage[lit]

    return Image.fromarray(out.round().clip(0, 255).astype("uint8"), "RGBA")


#: How much transparent room a mark keeps around its disc, as a fraction
#: of its width.
#:
#: Without it the artwork runs into the bitmap border and there is nowhere
#: for the outline to fade: upstream ships discs inscribed exactly in the
#: square, so at the four mid-sides the edge is a *cut* -- 140 fully
#: opaque pixels sitting on the border of one chain mark, with no alpha
#: between the colour and the end of the file. That reads as cropped,
#: because it is, and it leaves any filter sampling across the boundary
#: with nothing sensible to average.
DISC_MARGIN = 0.02

#: How finely the outline is sampled before being averaged down into the
#: alpha channel. 8x8 per pixel, so a pixel the circle half covers gets an
#: alpha near 128 rather than 0 or 255 -- which is what antialiasing *is*,
#: and what a 1-pixel ramp was not.
DISC_SUPERSAMPLE = 8


@lru_cache(maxsize=4)
def disc_alpha(size: int):
    """A circular coverage mask, 0.0 to 1.0, antialiased at its edge.

    Coverage rather than a threshold: each pixel is the fraction of it
    that falls inside the circle, computed by sampling and averaging.
    Cached because it depends on nothing but the size, and every mark in
    the build shares it.
    """
    import numpy as np

    s = DISC_SUPERSAMPLE
    fine = size * s
    # Pixel centres in the fine grid, in units of the output pixel.
    axis = (np.arange(fine) + 0.5) / s
    dx = axis - size / 2
    inside = (dx[None, :] ** 2 + dx[:, None] ** 2) <= (
        size / 2 * (1 - DISC_MARGIN)
    ) ** 2
    return inside.reshape(size, s, size, s).mean(axis=(1, 3))


def round_off(image):
    """Cut the mark to a disc *in the alpha channel*, with a soft edge.

    The app draws every one of these as a circle. Doing that at the point
    of drawing means the renderer's clip decides how the outline looks,
    and on WebKit at a high pixel ratio it decided badly. Doing it here
    means the outline is alpha, in the file, identical everywhere -- and
    a renderer that samples across it finds a gradient rather than a
    cliff.

    Multiplied into whatever alpha the artwork already has, so a logo
    that was already round keeps its own edge and a square one gains one.
    """
    import numpy as np
    from PIL import Image

    square = image.convert("RGBA")
    width, height = square.size
    if width != height:
        # A couple of dozen of these arrive a pixel off square -- 160x159,
        # 49x50. Centred on a transparent square rather than skipped,
        # because skipping them is what left marks with a cut edge.
        side = max(width, height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(square, ((side - width) // 2, (side - height) // 2))
        square = canvas

    rgba = np.asarray(square).astype(np.float64)
    rgba[..., 3] *= disc_alpha(rgba.shape[0])
    return Image.fromarray(rgba.round().clip(0, 255).astype("uint8"), "RGBA")


def pad_full_bleed(path: Path, target: Path) -> bool:
    """Copy a token image, giving a full-bleed one room to be a circle.

    Most token logos are round artwork on transparency and are copied
    untouched -- clipping those to a circle removes nothing. A logo whose
    corners are *opaque* was drawn as a square, and that is the one that
    needs the padding.
    """
    from PIL import Image

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        spots = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        corners = [rgba.getpixel(spot) for spot in spots]
        if any(not isinstance(pixel, tuple) or pixel[3] == 0 for pixel in corners):
            round_off(shrink(rgba)).save(target, optimize=True)
            return False

        pad = round(width * BLEED_PAD)
        # The background to extend is whatever the corner is -- these are
        # flat-backed logos, which is why they are square in the first
        # place. Padded before shrinking, so the padding is resampled with
        # the artwork rather than added at a size it was not measured for.
        canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), corners[0])
        canvas.paste(rgba, (pad, pad), rgba)
        round_off(shrink(canvas)).save(target, optimize=True)
    return True


def shrink_file(path: Path, target: Path) -> None:
    """Copy one image, no larger than a compiled mark needs to be."""
    from PIL import Image

    with Image.open(path) as image:
        round_off(shrink(image.convert("RGBA"))).save(target, optimize=True)


def copy_tree(source: Path, target: Path, *, tokens: bool = False) -> tuple[int, int]:
    if not source.is_dir():
        return 0, 0
    target.mkdir(parents=True, exist_ok=True)
    files = size = padded = 0
    for item in source.iterdir():
        if not item.is_file():
            continue
        destination = target / item.name
        if tokens and item.suffix.lower() == ".png":
            padded += pad_full_bleed(item, destination)
        elif item.suffix.lower() == ".png":
            # Chain marks. Drawn smallest of anything here -- 14px in the
            # picker -- so they had the furthest to fall and looked the
            # worst for it.
            shrink_file(item, destination)
        else:
            shutil.copy2(item, destination)
        files += 1
        size += destination.stat().st_size
    if padded:
        print(f"  padded {padded} full-bleed logo(s) in {target.name}")
    return files, size


#: What this script cannot run without, as `(module, what to install)`.
#: Both do the actual work -- Pillow opens and writes every mark, numpy
#: averages the resampling -- so neither has a degraded mode worth having.
BUILD_REQUIREMENTS = (("PIL", "Pillow"), ("numpy", "numpy"))


def missing_requirements() -> list[str]:
    """Which build-time libraries are not installed.

    `find_spec` rather than an import, so asking the question costs nothing
    when the answer is yes.
    """
    return [
        package
        for module, package in BUILD_REQUIREMENTS
        if importlib.util.find_spec(module) is None
    ]


def main() -> int:
    # Before anything else, and fatal -- which it was not, and that is the
    # whole reason this check exists.
    #
    # These used to be optional: a missing Pillow fell through to
    # `shutil.copy2` and the art was copied at whatever size upstream drew
    # it. The build then *succeeded*, printed its usual summary, and left
    # 200px marks where the app draws 38 -- the tenfold reduction
    # `ui.assets.MARK_PIXELS` exists to prevent, and 29 MB of assets rather
    # than 19. Nothing said so; the only thing that noticed was
    # `tests/test_assets.py`, and only if somebody ran it.
    #
    # A build tool that quietly produces the wrong output is worse than one
    # that refuses, because the wrong output gets published. `build_icons.py`
    # already refuses when Pillow or librsvg is missing; this now matches it.
    missing = missing_requirements()
    if missing:
        print(
            f"{', '.join(missing)} not installed -- the marks would be copied "
            "at upstream's size rather than compiled.\n"
            "Install the build tools: uv pip install -r pyproject.toml --group dev",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chains",
        nargs="*",
        default=None,
        help="chains to copy token images for (default: every one upstream has)",
    )
    options = parser.parse_args()

    if not SOURCE.is_dir():
        print(
            f"{SOURCE} is missing. Run: git submodule update --init",
            file=sys.stderr,
        )
        return 1

    chains = options.chains or available_chains(SOURCE)
    if not chains:
        print(f"No token images under {SOURCE / 'images'}", file=sys.stderr)
        return 1

    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)

    total = 0

    # The Curve mark for the header. Only the wordless logo is taken.
    branding = TARGET / "branding"
    branding.mkdir()
    logo = SOURCE / "branding" / "logo.svg"
    if logo.is_file():
        shutil.copy2(logo, branding / "logo.svg")
        total += logo.stat().st_size
        print(f"  branding/logo.svg  {logo.stat().st_size / 1024:.0f} KB")

    files, size = copy_tree(SOURCE / "chains", TARGET / "chains")
    total += size
    print(f"  chains/            {files} files, {size / 1024:.0f} KB")

    for chain in chains:
        files, size = copy_tree(
            SOURCE / "images" / token_dir(chain), TARGET / "tokens" / chain, tokens=True
        )
        total += size
        if files:
            print(f"  tokens/{chain:<12} {files} files, {size / 1024 / 1024:.1f} MB")
        else:
            print(f"  tokens/{chain:<12} nothing upstream — will draw initials")

    print(
        f"\n{TARGET.relative_to(ROOT)}: {total / 1024 / 1024:.1f} MB "
        f"across {len(chains)} chains"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
