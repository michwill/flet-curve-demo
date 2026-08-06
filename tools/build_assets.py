#!/usr/bin/env python3
"""Compile the subset of curve-assets this app needs into `src/assets`.

The upstream repo is 67 MB across 38 networks. Copying it wholesale would
put all of that into every `flet publish` output, so this takes only what
the app can actually show: the chain logos (388 KB for all 40, small enough
to take entirely), the Curve mark, and the token images for the chains the
chain picker offers.

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

#: Chains whose token images are worth carrying. Keep in step with
#: `PREFERRED_CHAINS` in main.py -- a chain the picker offers but has no
#: images for still works, it just draws lettered circles.
DEFAULT_CHAINS = ("ethereum", "arbitrum", "base", "optimism", "polygon", "fraxtal")

#: Upstream puts Ethereum in `images/assets` and everything else in
#: `images/assets-<chain>`.
def token_dir(chain: str) -> str:
    return "assets" if chain == "ethereum" else f"assets-{chain}"


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
        default=list(DEFAULT_CHAINS),
        help="chains to copy token images for",
    )
    options = parser.parse_args()

    if not SOURCE.is_dir():
        print(
            f"{SOURCE} is missing. Run: git submodule update --init",
            file=sys.stderr,
        )
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

    for chain in options.chains:
        files, size = copy_tree(
            SOURCE / "images" / token_dir(chain), TARGET / "tokens" / chain
        )
        total += size
        if files:
            print(f"  tokens/{chain:<12} {files} files, {size / 1024 / 1024:.1f} MB")
        else:
            print(f"  tokens/{chain:<12} nothing upstream — will draw initials")

    print(f"\n{TARGET.relative_to(ROOT)}: {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
