"""Pull the published site back through the gateways, from time to time."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ens import EnsError, contenthash, name_behind
from tools.publish_ipfs import (
    DIST,
    WARM_WORKERS,
    ProgressReporter,
    boot_files,
    elapsed_text,
    resolved_cid,
    verify,
)

#: Both of the ENS names, and the public gateways beside them.
#:
#: Not because a visitor chooses -- they do not -- but because each gateway
#: fetches from the network on its own, and the one that has never been asked
#: is the one that answers 504.  A report of missing marks came from a build
#: whose CID served them fine here: the reporter's edge had them, the
#: reporter's neighbours' edge did not.
#:
#: `.limo` and `.link` answer with the same `server: eth.limo`, so they are
#: one operator rather than two, and their `cache-control` is 300 seconds --
#: which is what this can and cannot buy.  Warming does not fill a cache for
#: long; what lasts is the gateway's node having seen the blocks.
#:
#: The CID-addressed ones are formatted with the published CID, which is why
#: they carry a `{cid}` rather than being usable as they stand.
GATEWAYS = ("https://curve.eth.limo", "https://curve.eth.link")

#: The staging name, and the same two operators.  A separate build behind a
#: separate contenthash, so a warm of one says nothing about the other: the
#: gateway's node has to have seen *these* blocks.  `name_behind` reads the
#: name off the host, so nothing else has to know this one exists.
STAGING_GATEWAYS = (
    "https://staging.curve.eth.limo",
    "https://staging.curve.eth.link",
)

#: Filled in with the CID being warmed.  These serve any CID, so a name is no
#: use to them.
CID_GATEWAYS = (
    "https://ipfs.io/ipfs/{cid}",
    "https://{cid}.ipfs.dweb.link",
    "https://{cid}.ipfs.w3s.link",
)

#: How long to wait for a gateway to notice the name has moved, and how
#: often to ask. Their own ENS lookups are cached for minutes, so a warm
#: started the moment the transaction lands would warm the old build --
#: which is what happened, and looked like the warm having no effect.
FLIP_DEADLINE = 900.0
FLIP_INTERVAL = 20.0

#: Where the compiled marks live inside the build, and how they are named.
MARKS_DIR = ("curve", "tokens")

#: Which sizes to warm unless told otherwise.
DEFAULT_TIERS = (40, 80)

#: Longer than a publish's, because nothing is waiting on this.
WARM_DEADLINE = 7200.0

#: Workers for the marks, against `publish_ipfs`'s two.
MARK_WORKERS = 8

#: How many files to ask for between progress lines.
CHUNK = 64

#: How long to keep retrying inside one batch before moving on.
CHUNK_DEADLINE = 120.0


def mark_files(root: Path, tiers: tuple[int, ...], chains: list[str]) -> list[str]:
    """Every compiled token mark, as paths relative to the site root."""
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


def bundle_files(root: Path, tiers: tuple[int, ...], chains: list[str]) -> list[str]:
    """Each chain's packed marks and its index, as site-root paths."""
    base = root.joinpath(*MARKS_DIR)
    found = []
    directories = [root / MARKS_DIR[0] / "chains"] if not chains else []
    if base.is_dir():
        directories += [p for p in sorted(base.iterdir()) if p.is_dir()]
    for chain in directories:
        if chains and chain.name not in chains:
            continue
        if not chain.is_dir():
            continue
        for tier in tiers:
            for infix in ("", "-rest"):
                for suffix in (".bin", ".json"):
                    path = chain / f"marks@{tier}{infix}{suffix}"
                    if path.is_file():
                        found.append(path.relative_to(root).as_posix())
    return found


def chain_files(root: Path) -> list[str]:
    """Every network mark's own file, at every tier."""
    base = root / MARKS_DIR[0] / "chains"
    if not base.is_dir():
        return []
    return [
        path.relative_to(root).as_posix()
        for path in sorted(base.iterdir())
        if path.suffix == ".png"
    ]


def brand_files(root: Path) -> list[str]:
    """The Curve mark itself, and anything else committed under `branding/`."""
    base = root / MARKS_DIR[0] / "branding"
    if not base.is_dir():
        return []
    return [
        path.relative_to(root).as_posix() for path in sorted(base.iterdir())
        if path.is_file()
    ]


def plan(root: Path, options) -> list[str]:
    """What to ask for: the boot set and the mark bundles, in that order."""
    paths: list[str] = boot_files(root) if options.boot else []
    if options.boot_only:
        return paths
    paths += bundle_files(root, options.tiers, options.chains)
    if not options.chains:
        paths += brand_files(root)
        paths += chain_files(root)
    if options.all_marks:
        paths += mark_files(root, options.tiers, options.chains)
    return paths


def batched(paths: list[str], size: int) -> list[list[str]]:
    """The paths in runs of `size`, order preserved."""
    return [paths[i : i + size] for i in range(0, len(paths), size)]


def remaining_text(done: int, total: int, elapsed: float) -> str:
    """A rough "still to go", from the rate so far."""
    if not done or done >= total:
        return ""
    left = elapsed / done * (total - done)
    return f"  ~{elapsed_text(left)} left"


def weight(root: Path, paths: list[str]) -> int:
    """How many bytes this run will pull, per gateway."""
    return sum((root / p).stat().st_size for p in paths if (root / p).exists())


def rate_text(done: int, total: int, done_bytes: int, elapsed: float) -> str:
    """Throughput so far and what is left of it, measured not predicted."""
    if elapsed <= 0 or not done:
        return ""
    left = (total - done) / (done / elapsed)
    return (
        f"  {done / elapsed:.1f} files/s  {done_bytes / elapsed / 1024:,.0f} KB/s"
        f"  ~{elapsed_text(left)} left"
    )


def wanted_cid(host: str, options, *, say=print) -> str:
    """What the name behind this gateway points at, per the registry.

    Read from Ethereum rather than from a gateway, because the question is
    whether the gateway is up to date and it cannot be its own witness.
    """
    if getattr(options, "cid", ""):
        return str(options.cid)
    name = name_behind(host)
    if not name or getattr(options, "no_wait", False):
        return ""
    try:
        cid = contenthash(name)
    except EnsError as exc:
        say(f"  could not read {name} from Ethereum, warming anyway: {exc}")
        return ""
    if not cid:
        say(f"  {name} has no IPFS contenthash, warming whatever is served")
    return cid


def _published_cid(hosts: list[str]) -> str:
    """What the ENS names point at, for the gateways that need it spelled out."""
    for host in hosts:
        name = name_behind(host)
        if not name:
            continue
        try:
            if cid := contenthash(name):
                return cid
        except EnsError:
            continue
    return ""


def wait_for_flip(
    host: str,
    cid: str,
    options,
    *,
    client=None,
    now=time.monotonic,
    sleep=time.sleep,
    say=print,
) -> bool:
    """Block until `host` serves `cid`. True if it got there."""
    import httpx

    owned = client is None
    client = client or httpx.Client(follow_redirects=True)
    started = now()
    try:
        while True:
            live = resolved_cid(client, host)
            if live == cid:
                if now() - started > FLIP_INTERVAL:
                    say(f"  it is serving that now, after {elapsed_text(now() - started)}")
                return True
            waited = elapsed_text(now() - started)
            say(f"  {host} is still on {live or 'nothing readable'}   {waited}")
            if now() - started >= options.flip_deadline:
                say(
                    f"  giving up on the wait after {waited} and warming what it\n"
                    "  serves. Run this again once it has caught up."
                )
                return False
            sleep(FLIP_INTERVAL)
    finally:
        if owned:
            client.close()


def warm_one(host: str, paths: list[str], options) -> dict:
    """One gateway, whole files, two at a time."""
    total_bytes = weight(options.dist, paths)
    print(
        f"\n{host}: {len(paths)} files, {total_bytes / 1e6:.1f} MB, "
        f"{options.workers} at a time"
    )
    started = time.monotonic()
    report = ProgressReporter()
    bad: dict = {}
    done = done_bytes = 0
    for batch in batched(paths, options.chunk):
        bad.update(
            verify(
                "",
                batch,
                gateway=host,
                deadline=min(CHUNK_DEADLINE, options.deadline),
                workers=options.workers,
                whole=True,
            )
        )
        done += len(batch)
        done_bytes += weight(options.dist, batch)
        elapsed = time.monotonic() - started
        report(done - len(bad), len(paths), elapsed)
        if report.inline:
            sys.stdout.write(rate_text(done, len(paths), done_bytes, elapsed))
            sys.stdout.flush()
        if elapsed >= options.deadline:
            print(f"\n  stopping at the {elapsed_text(options.deadline)} deadline, "
                  f"{len(paths) - done} files not reached")
            break
    if report.inline:
        print()
    if bad:
        # The ask is what makes the content arrive: a gateway answers 504 when
        # it has started fetching and run out of patience, not when it has
        # decided against it.  Measured against ipfs.io on a live CID -- ten
        # of 305 timed out at 28s each on the first pass and every one of them
        # served in under a second when asked again.  So the stragglers get a
        # second pass before they are called failures.
        print(f"  {len(bad)} timed out; asking again now the fetch has started")
        again = verify("", sorted(bad), gateway=host,
                       deadline=min(CHUNK_DEADLINE, options.deadline),
                       workers=options.workers, whole=True)
        landed = len(bad) - len(again)
        if landed:
            print(f"  {landed} of them landed on the second ask")
        bad = again
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


def build_parser() -> argparse.ArgumentParser:
    """The command line, apart from `main`, so it can be read without a warm."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=DIST, help="the build to warm from")
    parser.add_argument(
        "--gateway",
        action="append",
        dest="gateways",
        help="warm this host (repeatable). Defaults to eth.limo and eth.link.",
    )
    parser.add_argument(
        "--staging",
        action="store_true",
        help="warm staging.curve.eth instead of curve.eth",
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
        "--no-boot",
        dest="boot",
        action="store_false",
        help="skip the boot set (59 files, 21 MB) and warm only the marks",
    )
    parser.add_argument("--boot-only", action="store_true", help="only the boot set")
    parser.add_argument(
        "--all-marks",
        action="store_true",
        help="also warm the 3,358 individual marks behind the bundles",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=WARM_DEADLINE,
        help="seconds to spend per gateway before giving up on the rest",
    )
    parser.add_argument(
        "--chunk", type=int, default=CHUNK, help="files between progress lines"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help=f"parallel requests (default {MARK_WORKERS}, or "
        f"{WARM_WORKERS} when the boot set is included)",
    )
    parser.add_argument("--show", type=int, default=20, help="failures to list")
    parser.add_argument(
        "--cid",
        default="",
        help="warm this CID rather than whatever the registry says the name "
        "points at",
    )
    parser.add_argument(
        "--no-cid-gateways",
        action="store_true",
        help="only the ENS names, not the public gateways beside them",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="do not read ENS or wait for the gateway to catch up with it",
    )
    parser.add_argument(
        "--flip-deadline",
        type=float,
        default=FLIP_DEADLINE,
        help=f"seconds to wait for a gateway to notice (default {FLIP_DEADLINE:.0f})",
    )
    return parser


def main() -> int:
    options = build_parser().parse_args()

    if options.boot_only:
        options.boot = True
    if not options.workers:
        options.workers = WARM_WORKERS if options.boot else MARK_WORKERS

    options.tiers = parse_tiers(options.tiers)
    options.chains = [c for c in options.chains.replace(",", " ").split() if c]
    named = STAGING_GATEWAYS if options.staging else GATEWAYS
    hosts = [h.rstrip("/") for h in (options.gateways or named)]
    if not options.gateways and not options.no_cid_gateways:
        # The public ones too, addressed by CID.  Each gateway fetches from the
        # network on its own, so the one nobody has asked is the one that
        # answers 504 -- which is what a visitor arriving through it meets.
        cid = options.cid or _published_cid(hosts)
        if cid:
            hosts += [g.format(cid=cid) for g in CID_GATEWAYS]
        else:
            print("  no CID to address the public gateways by; warming the "
                  "ENS names only")

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
            if cid := wanted_cid(host, options):
                print(f"\n{name_behind(host)} points at {cid}")
                wait_for_flip(host, cid, options)
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
