"""Pull the published site back through the gateways, from time to time.

`publish_ipfs.py` warms once, at publish, and that is not enough. Warming
**decays**: `main.dart.wasm` is in the boot set and was warmed on the 15th,
and on the 18th `curve.eth.limo` answered 504 for it after seventeen
seconds -- the signature of a gateway that could not find the block, not
one refusing to serve it. An edge's store is a cache, several edges sit
behind one name, and a visitor arriving on a cold one gets the same coin
flip whatever happened at publish.

So this is the same act as `publish_ipfs --warm`, minus the publishing,
and pointed at more of the site:

  * **both gateways.** eth.limo and eth.link are separate infrastructure
    with separate caches, and warming one says nothing about the other.
    Whichever a visitor reaches is not our choice;
  * **the token marks**, which publishing deliberately leaves alone. See
    `LAZY_DIR`: 6,716 files is an imposition to check on every publish, and
    that reasoning is about *publishing*. A job that runs occasionally can
    afford what a job that runs on every deploy cannot -- and those files
    are precisely the ones nobody has ever warmed, which is why a missing
    coin logo is the most visible form this bug takes.

Nothing here changes what is published. It only asks for files that are
already there, which is why it is safe to run at any time, as often as
patience allows, and why an interrupted run costs nothing: every file
fetched before the interrupt stays fetched.

The file list comes from the local `dist/`, so a `dist/` that has drifted
from what is pinned will ask for paths the gateway does not have. Those
come back as `refused` with a 404, which is a true statement about the
published site rather than a fault here -- but it means the honest way to
read a 404 from this script is "rebuild, or you are warming the wrong
list".

    tools/warm_ipfs.py                    # boot set + marks, both gateways
    tools/warm_ipfs.py --marks-only       # just the logos
    tools/warm_ipfs.py --tiers all        # every compiled size
    tools/warm_ipfs.py --gateway https://curve.eth.limo
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.publish_ipfs import (
    DIST,
    WARM_WORKERS,
    boot_files,
    elapsed_text,
    progress_reporter,
    verify,
)

#: Both of them, because they are separate infrastructure behind one name
#: and a visitor does not choose between them. eth.link is Cloudflare's
#: resolver and eth.limo is independent; a block warm in one is not warm in
#: the other, so warming a single gateway leaves half the audience where it
#: started.
GATEWAYS = ("https://curve.eth.limo", "https://curve.eth.link")

#: Where the compiled marks live inside the build, and how they are named.
#: `<root>/curve/tokens/<chain>/<address>@<tier>.png` -- see
#: `tools/build_assets.py`, which writes them, and `ui/assets.py`, which
#: picks the tier from the screen's pixel ratio.
MARKS_DIR = ("curve", "tokens")

#: Which sizes to warm unless told otherwise.
#:
#: Not all four. `ui.assets.mark_tier` rounds *up* from the mark's size in
#: device pixels, and a mark is drawn between 14 and 38 logical pixels: at
#: the ratios real screens report, that lands on 40 or 80 nearly always. 20
#: needs a 1x screen and the smallest mark on the page; 160 needs a 4x one.
#: Warming those two doubles the work to cover the ends of the range, so
#: they are opt-in through `--tiers all`.
DEFAULT_TIERS = (40, 80)

#: Longer than a publish's, because nothing is waiting on this. A publish
#: verifies while somebody watches; this runs on its own and the only cost
#: of a slow round is that it is still going.
WARM_DEADLINE = 3600.0


def mark_files(root: Path, tiers: tuple[int, ...], chains: list[str]) -> list[str]:
    """Every compiled token mark, as paths relative to the site root.

    Sorted by chain and then by name so two runs ask in the same order:
    an interrupted run then resumes over roughly the same ground rather
    than sampling a fresh scatter of a 6,716-file set.
    """
    base = root.joinpath(*MARKS_DIR)
    if not base.is_dir():
        return []
    wanted = {f"@{tier}.png" for tier in tiers}
    found = []
    for chain in sorted(p for p in base.iterdir() if p.is_dir()):
        if chains and chain.name not in chains:
            continue
        found += [
            path.relative_to(root).as_posix()
            for path in sorted(chain.iterdir())
            if any(path.name.endswith(suffix) for suffix in wanted)
        ]
    return found


def plan(root: Path, options) -> list[str]:
    """What to ask for, boot set first.

    Order matters on an interrupted run: the boot set is what decides
    whether the site loads at all, and the marks only decide whether it
    looks right. Ctrl-C after ten minutes should have bought the first.
    """
    paths: list[str] = [] if options.marks_only else boot_files(root)
    if not options.boot_only:
        paths += mark_files(root, options.tiers, options.chains)
    return paths


def warm_one(host: str, paths: list[str], options) -> dict:
    """One gateway, whole files, two at a time. Returns what is still bad."""
    print(f"\n{host}: {len(paths)} files, {WARM_WORKERS} at a time")
    started = time.monotonic()
    report = progress_reporter()
    bad = verify(
        "",
        paths,
        gateway=host,
        deadline=options.deadline,
        workers=WARM_WORKERS,
        whole=True,
        on_round=report,
    )
    if report.inline:
        print()
    took = elapsed_text(time.monotonic() - started)
    if not bad:
        print(f"  all {len(paths)} served after {took}")
    else:
        print(f"  {len(bad)} of {len(paths)} still not served after {took}:")
        for path, (verdict, status, seconds) in sorted(bad.items())[: options.show]:
            print(f"    {verdict:>9}  {status!s:>12}  {seconds:6.2f}s  {path}")
        if len(bad) > options.show:
            print(f"    ... and {len(bad) - options.show} more")
    return bad


def parse_tiers(raw: str) -> tuple[int, ...]:
    if raw.strip().lower() == "all":
        from ui.assets import MARK_TIERS

        return MARK_TIERS
    return tuple(int(part) for part in raw.replace(",", " ").split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=DIST, help="the build to warm from")
    parser.add_argument(
        "--gateway",
        action="append",
        dest="gateways",
        help="warm this host (repeatable). Defaults to eth.limo and eth.link.",
    )
    parser.add_argument(
        "--tiers",
        default=",".join(str(t) for t in DEFAULT_TIERS),
        help='mark sizes to warm, or "all" (default: 40,80)',
    )
    parser.add_argument(
        "--chains", default="", help="only these chains' marks, comma separated"
    )
    parser.add_argument("--boot-only", action="store_true", help="skip the marks")
    parser.add_argument("--marks-only", action="store_true", help="skip the boot set")
    parser.add_argument("--deadline", type=float, default=WARM_DEADLINE, help="seconds")
    parser.add_argument("--show", type=int, default=20, help="failures to list")
    options = parser.parse_args()

    if options.boot_only and options.marks_only:
        parser.error("--boot-only and --marks-only ask for nothing at all")

    options.tiers = parse_tiers(options.tiers)
    options.chains = [c for c in options.chains.replace(",", " ").split() if c]
    hosts = [h.rstrip("/") for h in (options.gateways or GATEWAYS)]

    root = options.dist
    if not root.is_dir():
        print(f"no build at {root} -- run tools/publish_ipfs.py --no-upload first")
        return 2

    paths = plan(root, options)
    if not paths:
        print(f"nothing to warm under {root}")
        return 2

    print(f"warming {len(paths)} files from {root} through {len(hosts)} gateway(s)")
    print("Ctrl-C is safe: whatever has been fetched stays fetched.")
    started = time.monotonic()
    left = 0
    try:
        for host in hosts:
            left += len(warm_one(host, paths, options))
    except KeyboardInterrupt:
        print(
            f"\n\nstopped after {elapsed_text(time.monotonic() - started)}. "
            "What was fetched stays warm;\nrun it again to carry on."
        )
        return 130

    print(f"\ndone in {elapsed_text(time.monotonic() - started)}")
    if left:
        print(
            "  Some files did not land. Each pass leaves behind what it managed\n"
            "  to fetch, so running it again usually shrinks the list. A 404 is\n"
            "  different: that is a file the published site does not have, and\n"
            "  means this dist/ has drifted from what is pinned."
        )
    return 1 if left else 0


if __name__ == "__main__":
    raise SystemExit(main())
