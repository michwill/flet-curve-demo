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


def copy_tree(source: Path, target: Path) -> tuple[int, int]:
    if not source.is_dir():
        return 0, 0
    target.mkdir(parents=True, exist_ok=True)
    files = size = 0
    for item in source.iterdir():
        if not item.is_file():
            continue
        shutil.copy2(item, target / item.name)
        files += 1
        size += item.stat().st_size
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
            SOURCE / "images" / token_dir(chain), TARGET / "tokens" / chain
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
