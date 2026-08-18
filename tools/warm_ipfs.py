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
WARM_DEADLINE = 7200.0

#: How many files to ask for between progress lines.
#:
#: `verify` reports once per *pass*, which is the right grain when the pass
#: is the 77-file boot set and takes forty-five seconds. It is the wrong
#: grain here. A full warm is 3,435 files, and measured at the ~1.7 files a
#: second two workers actually manage, one pass is **thirty-four minutes**
#: -- so the first version of this script printed its header and then
#: nothing at all for over half an hour, which is indistinguishable from a
#: hang and was reported as one.
#:
#: So the run is cut into batches and each batch reports. Sixty-four is
#: about forty seconds of work: often enough to see it moving, rare enough
#: that the bar is not the thing doing the work.
CHUNK = 64

#: How long to keep retrying inside one batch before moving on.
#:
#: Not the run's deadline, which would let a single unfindable file hold a
#: batch for the whole budget while 3,000 warm ones waited behind it. A
#: cold block answers 504 in ~17s, so this is a few attempts, and anything
#: still missing is reported and left -- there is another pass next time
#: the script runs, which is the entire premise.
CHUNK_DEADLINE = 120.0


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
    """What to ask for. The marks, unless the boot set is asked for too.

    **The marks are the default and the boot set is not**, which is the
    opposite of what this script started with, and the reason is bytes
    rather than taste. Measured on this build:

        boot set     77 files   60.7 MB
        marks      3,358 files  10.9 MB

    The boot set is 85% of the weight, it is warmed on every publish, and
    77 files of it are large enough that one slow gateway turns the run
    into an overnight job -- observed at 2 KB/s, which is eight hours for
    the boot set alone and ninety minutes for every mark on every chain.
    The marks are the half nothing else ever warms. So this warms those,
    and `--boot` adds the rest when that is what you want.

    Boot files still come first when they are included: they decide whether
    the site loads at all, where the marks only decide whether it looks
    right, and an interrupted run should have bought the first.
    """
    paths: list[str] = boot_files(root) if options.boot else []
    if not options.boot_only:
        paths += mark_files(root, options.tiers, options.chains)
    return paths


def batched(paths: list[str], size: int) -> list[list[str]]:
    """The paths in runs of `size`, order preserved."""
    return [paths[i : i + size] for i in range(0, len(paths), size)]


def remaining_text(done: int, total: int, elapsed: float) -> str:
    """A rough "still to go", from the rate so far.

    Rough on purpose: the rate swings by an order of magnitude depending on
    how many blocks in a batch were cold, and a confident-looking countdown
    that is wrong by twenty minutes is worse than an approximate one.
    """
    if not done or done >= total:
        return ""
    left = elapsed / done * (total - done)
    return f"  ~{elapsed_text(left)} left"


def weight(root: Path, paths: list[str]) -> int:
    """How many bytes this run will pull, per gateway.

    Bytes rather than files, because files are a poor proxy here: 77 boot
    files outweigh 3,358 marks six to one, so a run reported purely in
    files sits at "0/3435" through the slowest part of its work and looks
    stuck. It is the number this script is actually rate-limited by.
    """
    return sum((root / p).stat().st_size for p in paths if (root / p).exists())


def rate_text(done_bytes: int, total_bytes: int, elapsed: float) -> str:
    """Throughput so far and what is left of it, measured not predicted.

    No estimate is printed before a run any more. Two measurements of the
    same gateway, hours apart, came in at 686 KB/s and 2 KB/s -- a spread
    of three hundred times -- and a prediction drawn from either would be
    a confident lie about the other. What it can honestly say is how fast
    it is going *now*, which self-corrects.
    """
    if elapsed <= 0 or not done_bytes:
        return ""
    rate = done_bytes / elapsed
    left = (total_bytes - done_bytes) / rate if rate else 0
    return f"  {rate / 1024:,.0f} KB/s  ~{elapsed_text(left)} left"


def warm_one(host: str, paths: list[str], options) -> dict:
    """One gateway, whole files, two at a time. Returns what is still bad.

    In batches, so it reports while it works rather than at the end -- see
    `CHUNK`. Batching also bounds how long one unfindable file can hold up
    the ones behind it, which at three thousand files is the difference
    between a slow job and a stalled one.
    """
    total_bytes = weight(options.dist, paths)
    print(
        f"\n{host}: {len(paths)} files, {total_bytes / 1e6:.1f} MB, "
        f"{WARM_WORKERS} at a time"
    )
    started = time.monotonic()
    report = progress_reporter()
    bad: dict = {}
    done = done_bytes = 0
    for batch in batched(paths, options.chunk):
        bad.update(
            verify(
                "",
                batch,
                gateway=host,
                deadline=min(CHUNK_DEADLINE, options.deadline),
                workers=WARM_WORKERS,
                whole=True,
            )
        )
        done += len(batch)
        done_bytes += weight(options.dist, batch)
        elapsed = time.monotonic() - started
        report(done - len(bad), len(paths), elapsed)
        if report.inline:
            sys.stdout.write(rate_text(done_bytes, total_bytes, elapsed))
            sys.stdout.flush()
        if elapsed >= options.deadline:
            print(f"\n  stopping at the {elapsed_text(options.deadline)} deadline, "
                  f"{len(paths) - done} files not reached")
            break
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
    parser.add_argument(
        "--boot",
        action="store_true",
        help="warm the boot set too (60 MB, and publishing already warms it)",
    )
    parser.add_argument("--boot-only", action="store_true", help="only the boot set")
    parser.add_argument(
        "--deadline",
        type=float,
        default=WARM_DEADLINE,
        help="seconds to spend per gateway before giving up on the rest",
    )
    parser.add_argument(
        "--chunk", type=int, default=CHUNK, help="files between progress lines"
    )
    parser.add_argument("--show", type=int, default=20, help="failures to list")
    options = parser.parse_args()

    if options.boot_only:
        options.boot = True

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

    options.dist = root
    total = weight(root, paths)
    print(
        f"warming {len(paths)} files ({total / 1e6:.1f} MB) from {root} "
        f"through {len(hosts)} gateway(s)"
    )
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
