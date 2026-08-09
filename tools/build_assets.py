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
import shutil
import sys
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
    alpha = rgba[..., 3:4] / 255.0
    rgba[..., :3] *= alpha

    width, height = image.size
    scale = target / max(width, height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    small = np.asarray(
        Image.fromarray(rgba.round().clip(0, 255).astype("uint8"), "RGBA").resize(
            size, Image.Resampling.BOX
        ),
        dtype=np.float64,
    )

    out_alpha = small[..., 3:4] / 255.0
    lit = out_alpha[..., 0] > 0
    small[lit, :3] /= out_alpha[lit]
    return Image.fromarray(small.round().clip(0, 255).astype("uint8"), "RGBA")


def pad_full_bleed(path: Path, target: Path) -> bool:
    """Copy a token image, giving a full-bleed one room to be a circle.

    Most token logos are round artwork on transparency and are copied
    untouched -- clipping those to a circle removes nothing. A logo whose
    corners are *opaque* was drawn as a square, and that is the one that
    needs the padding.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a build-time tool
        shutil.copy2(path, target)
        return False

    with Image.open(path) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        spots = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        corners = [rgba.getpixel(spot) for spot in spots]
        if any(not isinstance(pixel, tuple) or pixel[3] == 0 for pixel in corners):
            shrink(rgba).save(target, optimize=True)
            return False

        pad = round(width * BLEED_PAD)
        # The background to extend is whatever the corner is -- these are
        # flat-backed logos, which is why they are square in the first
        # place. Padded before shrinking, so the padding is resampled with
        # the artwork rather than added at a size it was not measured for.
        canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), corners[0])
        canvas.paste(rgba, (pad, pad), rgba)
        shrink(canvas).save(target, optimize=True)
    return True


def shrink_file(path: Path, target: Path) -> None:
    """Copy one image, no larger than a compiled mark needs to be."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a build-time tool
        shutil.copy2(path, target)
        return
    with Image.open(path) as image:
        shrink(image.convert("RGBA")).save(target, optimize=True)


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


def main() -> int:
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
