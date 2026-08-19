#!/usr/bin/env python3
"""Pin the web build to IPFS, through Pinata.

**The Pinata JWT goes in `local_secrets.toml` at the repo root, or in
`PINATA_JWT` -- never under `src/`**, which `flet publish` tars into the app
and this pins to IPFS, where there is no delete button. `leaked` searches the
build for it before every upload.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# Run as `python tools/publish_ipfs.py`, the interpreter puts `tools/` on the
# path and not the repo root, so `from tools import ...` cannot resolve --
# while the tests, which import this as `tools.publish_ipfs`, resolve it
# fine.
if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(ROOT))

#: Outside `src/`, and outside git. Both on purpose -- see the module note.
SECRETS = ROOT / "local_secrets.toml"

#: What `flet publish` tars into the app, and therefore what must be clean
#: before it runs.
SOURCE = ROOT / "src"

#: Compiled bytecode, which goes up unless something stops it.
BYTECODE = "__pycache__"

PIN_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"

#: Where to send someone once it is pinned.
PUBLIC_GATEWAY = "https://dweb.link/ipfs/"

#: CIDv1 rather than v0: it is case-insensitive base32, which is what a
#: subdomain gateway (`https://<cid>.ipfs.dweb.link`) needs to put the CID
#: in a hostname.
CID_VERSION = 1

#: Read size per file, and how often to report progress.
CHUNK = 1 << 20
REPORT_EVERY = 8 << 20

HOW_TO_CONFIGURE = f"""\
No Pinata credentials. Either:

    export PINATA_JWT='...'

or put them in {SECRETS.name} at the repo root (gitignored):

    [pinata]
    jwt = "..."

An API key with `pinFileToIPFS` permission makes the JWT:
https://app.pinata.cloud/developers/api-keys

Do NOT put it in src/local_config.toml -- everything under src/ is bundled
into the published app and would be pinned along with it."""


# -- credentials -----------------------------------------------------------


def config() -> dict:
    """`local_secrets.toml`, or an empty dict if there is none."""
    try:
        return tomllib.loads(SECRETS.read_text(encoding="utf-8")).get("pinata", {})
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"{SECRETS.name}: {exc}") from exc


def token(values: dict | None = None) -> str:
    """The JWT: environment first, so CI needs no file."""
    from_env = os.environ.get("PINATA_JWT", "").strip()
    if from_env:
        return from_env
    jwt = str((config() if values is None else values).get("jwt", "")).strip()
    if not jwt:
        raise SystemExit(HOW_TO_CONFIGURE)
    return jwt


# -- making the build work anywhere but a site root ------------------------

BASE_ABSOLUTE = '<base href="/">'
BASE_RELATIVE = '<base href="./">'

#: Routes must live in the fragment, or every deep link 404s at the gateway.
ROUTE_STRATEGY = "hash"

#: index.html names the strategy twice: Flet's template default, and then
#: the value `flet publish` patches in below it.
_ROUTE_NAMED = re.compile(r'routeUrlStrategy\s*[:=]\s*"(path|hash)"')


def route_strategy(index: Path) -> str:
    """Which strategy the built page will actually use, or "" if unstated."""
    named = _ROUTE_NAMED.findall(index.read_text(encoding="utf-8"))
    return named[-1] if named else ""


def make_relative(index: Path) -> bool:
    """Point the page at its own directory rather than at the site root."""
    html = index.read_text(encoding="utf-8")
    if BASE_RELATIVE in html:
        return False
    if BASE_ABSOLUTE not in html:
        raise SystemExit(
            f"{index}: expected {BASE_ABSOLUTE} to make relative and found neither "
            f"it nor {BASE_RELATIVE}. Flet's index patcher has changed shape."
        )
    index.write_text(html.replace(BASE_ABSOLUTE, BASE_RELATIVE), encoding="utf-8")
    return True


# -- the app package, in something a gateway will hand over ----------------

#: What `flet publish` calls the Python app, and what it is turned into.
PACKAGE_FROM = "app.tar.gz"
PACKAGE_TO = "app-package.json"
#: The JSON key, named for what it holds -- so the worker unpacking it as a
#: gzipped tar is reading the file rather than assuming.
PACKAGE_KEY = "gztar"

#: Directories a CDN serves, so pinning them ships bytes nobody fetches.
CDN_SERVED = ("canvaskit", "pyodide")

#: How a suffix is tested, and why it costs nothing.
PROBE_ABSENT = "definitely-absent-file"

#: What a gateway says when it refuses a suffix outright, as opposed to
#: resolving the path and finding nothing there.
REFUSAL_BODY = "Resource Not Found"

#: Suffixes worth checking, for `--probe`. The allowed ones are in here as
#: controls: a run where *nothing* is allowed is a broken run rather than a
#: strict gateway, and without them there would be no way to tell.
PROBE_SUFFIXES = (
    ".bin", ".png", ".json", ".dat", ".pack", ".whl", ".gz", ".tar.gz",
    ".zip", ".tar", ".tgz", ".7z", ".rar", ".bz2", ".xz", ".zst", ".jar",
)

#: Suffixes an IPFS gateway refuses outright.
REFUSED_SUFFIXES = (".zip", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar", ".zst", ".jar")


def wrap_package(root: Path) -> bool:
    """Turn the app archive into base64 in JSON, and repoint the page."""
    archive = root / PACKAGE_FROM
    if not archive.is_file():
        return False
    index = root / "index.html"
    html = index.read_text(encoding="utf-8")
    if f'"{PACKAGE_FROM}"' not in html:
        raise SystemExit(
            f'{index}: no appPackageUrl: "{PACKAGE_FROM}" to repoint. Wrapping the '
            "archive alone would leave the app fetching one that is not there."
        )
    payload = base64.b64encode(archive.read_bytes()).decode("ascii")
    (root / PACKAGE_TO).write_text(json.dumps({PACKAGE_KEY: payload}), encoding="utf-8")
    archive.unlink()
    index.write_text(
        html.replace(f'"{PACKAGE_FROM}"', f'"{PACKAGE_TO}"'), encoding="utf-8"
    )
    return True


#: Where the worker decides what it fetched, and what goes in front of that
#: decision.
WORKER = "python-worker.js"
WORKER_ANCHOR = '            if _archive_path.endswith(".zip"):'
WORKER_PATCH = f'''            if _archive_path.endswith(".json"):
                # Base64 in JSON. See tools/publish_ipfs.py for why the
                # archive cannot be fetched as an archive.
                import base64 as _b64, io as _io, json as _json, tarfile as _tf
                _blob = _json.loads(await response.string())["{PACKAGE_KEY}"]
                with _tf.open(fileobj=_io.BytesIO(_b64.b64decode(_blob))) as _archive:
                    _archive.extractall(".")
                _archive_format = ""
            elif _archive_path.endswith(".zip"):'''
WORKER_UNPACK = "            await response.unpack_archive(format=_archive_format)"
WORKER_UNPACK_PATCHED = f"""            if _archive_format:
    {WORKER_UNPACK}"""


def patch_worker(root: Path) -> bool:
    """Teach the worker to read the wrapped package."""
    worker = root / WORKER
    source = worker.read_text(encoding="utf-8")
    if WORKER_PATCH.splitlines()[0] in source:
        return False
    if WORKER_ANCHOR not in source or WORKER_UNPACK not in source:
        raise SystemExit(
            f"{worker}: the archive branch is not where this expects it. Flet's "
            "worker has changed shape; the app package would be fetched and dropped."
        )
    worker.write_text(
        source.replace(WORKER_ANCHOR, WORKER_PATCH).replace(
            WORKER_UNPACK, WORKER_UNPACK_PATCHED
        ),
        encoding="utf-8",
    )
    return True


def cdn_build(index: Path) -> bool:
    """Does this build load canvaskit and Pyodide from a CDN?"""
    try:
        text = index.read_text(errors="ignore")
    except OSError:
        return True
    return "flet.noCdn=true" not in text.replace(" ", "")


def drop_cdn_copies(root: Path) -> list[tuple[str, int]]:
    """Delete the directories a CDN serves. Returns `(name, bytes)` freed."""
    if not cdn_build(root / "index.html"):
        return []
    freed = []
    for name in CDN_SERVED:
        directory = root / name
        if not directory.is_dir():
            continue
        size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        shutil.rmtree(directory)
        freed.append((name, size))
    return freed


def suffix_served(client, gateway: str, suffix: str) -> bool:
    """Would this gateway serve a file ending `suffix`, if one existed?"""
    url = f"{gateway.rstrip('/')}/{PROBE_ABSENT}{suffix}"
    try:
        body = client.get(url, timeout=VERIFY_TIMEOUT).text
    except Exception:
        return True
    return REFUSAL_BODY not in body


def probe_suffixes(gateway: str, suffixes=PROBE_SUFFIXES) -> dict[str, bool]:
    """Which of `suffixes` a gateway will serve."""
    import httpx

    with httpx.Client(follow_redirects=True) as client:
        return {s: suffix_served(client, gateway, s) for s in suffixes}


# -- the check that matters ------------------------------------------------

#: Where a secret could plausibly be readable.
TEXT_SUFFIXES = {".html", ".js", ".json", ".toml", ".txt", ".mjs", ".py"}
#: `.tgz` as well as `.gz`, because the app package is renamed to `.tgz`
#: before it is pinned -- and it is the one archive in the build that
#: actually contains `src/`, so missing it would gut the check.
ARCHIVE_SUFFIXES = {".gz", ".tgz", ".tar"}
#: Past this a file is canvaskit or a source map, not somewhere a key ends
#: up, and reading 37MB to prove it is time spent on nothing.
MAX_SCAN = 4 << 20


def leaked(root: Path, secret: str) -> list[str]:
    """Files in the build containing `secret`. Empty is the only good answer."""
    needle = secret.encode()
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix()
        if path.suffix in ARCHIVE_SUFFIXES and tarfile.is_tarfile(path):
            found += [f"{name}:{member}" for member in _in_tarball(path, needle)]
        elif (
            path.suffix in TEXT_SUFFIXES
            and path.stat().st_size <= MAX_SCAN
            and needle in path.read_bytes()
        ):
            found.append(name)
    package = app_archive(root)
    if package is not None:
        name, archive = package
        with archive:
            found += [
                f"{name}:{member}"
                for member in _members_holding(archive, needle)
                if f"{name}:{member}" not in found
            ]
    return sorted(found)


def app_archive(root: Path) -> tuple[str, tarfile.TarFile] | None:
    """The app's own archive, whichever shape the build has it in."""
    raw = root / PACKAGE_FROM
    if raw.is_file() and tarfile.is_tarfile(raw):
        return PACKAGE_FROM, tarfile.open(raw)
    wrapped = root / PACKAGE_TO
    if wrapped.is_file():
        try:
            blob = json.loads(wrapped.read_text(encoding="utf-8"))[PACKAGE_KEY]
            return PACKAGE_TO, tarfile.open(
                fileobj=io.BytesIO(base64.b64decode(blob))
            )
        except (ValueError, KeyError, tarfile.TarError):
            return None
    return None


def bytecode(root: Path) -> list[str]:
    """`__pycache__` in the build. Empty is the only good answer."""
    found = [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and BYTECODE in path.parts
    ]
    package = app_archive(root)
    if package is not None:
        name, archive = package
        with archive:
            found += [
                f"{name}:{member.name}"
                for member in archive.getmembers()
                if member.isfile() and BYTECODE in Path(member.name).parts
            ]
    return found


def refused_by_gateway(root: Path) -> list[str]:
    """Files in the build an IPFS gateway will not serve."""
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name.endswith(REFUSED_SUFFIXES)
    ]


def clear_bytecode() -> list[str]:
    """Delete every `__pycache__` under `src/`, and say which went."""
    gone = []
    for directory in sorted(SOURCE.rglob(BYTECODE)):
        if directory.is_dir():
            gone.append(directory.relative_to(ROOT).as_posix())
            shutil.rmtree(directory)
    return gone


def build_env() -> dict[str, str]:
    """The environment the build steps run in."""
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _in_tarball(path: Path, needle: bytes) -> list[str]:
    with tarfile.open(path) as archive:
        return _members_holding(archive, needle)


def _members_holding(archive: tarfile.TarFile, needle: bytes) -> list[str]:
    """Members of an open archive containing `needle`."""
    names = []
    for member in archive.getmembers():
        if not member.isfile() or member.size > MAX_SCAN:
            continue
        handle = archive.extractfile(member)
        if handle is not None and needle in handle.read():
            names.append(member.name)
    return names


# -- the request body ------------------------------------------------------


def uploads(root: Path, folder: str) -> list[tuple[str, Path]]:
    """Every file in the build, named the way Pinata rebuilds a directory."""
    return [
        (f"{folder}/{path.relative_to(root).as_posix()}", path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _file_head(boundary: str, name: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode()


def _field(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode()


def _tail(boundary: str) -> bytes:
    return f"--{boundary}--\r\n".encode()


def content_length(
    parts: list[tuple[str, Path]], fields: dict[str, str], boundary: str
) -> int:
    """How long the body will be, without building it."""
    total = sum(len(_field(boundary, key, value)) for key, value in fields.items())
    for name, path in parts:
        total += len(_file_head(boundary, name)) + path.stat().st_size + len(b"\r\n")
    return total + len(_tail(boundary))


def body(
    parts: list[tuple[str, Path]],
    fields: dict[str, str],
    boundary: str,
    on_progress=None,
) -> Iterator[bytes]:
    """The multipart body, one file at a time."""
    sent = 0
    for key, value in fields.items():
        chunk = _field(boundary, key, value)
        sent += len(chunk)
        yield chunk
    for name, path in parts:
        head = _file_head(boundary, name)
        sent += len(head)
        yield head
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                sent += len(chunk)
                if on_progress is not None:
                    on_progress(sent)
                yield chunk
        sent += 2
        yield b"\r\n"
    yield _tail(boundary)


def fields_for(name: str) -> dict[str, str]:
    return {
        "pinataMetadata": json.dumps({"name": name}),
        "pinataOptions": json.dumps({"cidVersion": CID_VERSION}),
    }


# -- the upload ------------------------------------------------------------


def pin(
    parts: list[tuple[str, Path]],
    fields: dict[str, str],
    jwt: str,
    *,
    timeout: float,
    client=None,
) -> dict:
    """Post the directory. Returns Pinata's JSON."""
    import httpx

    boundary = secrets.token_hex(16)
    total = content_length(parts, fields, boundary)
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(total),
    }

    def report(sent: int, _seen=[0]) -> None:  # noqa: B006 - a counter, not a default
        if sent - _seen[0] >= REPORT_EVERY:
            _seen[0] = sent
            print(f"  {sent / total:5.1%}  {sent >> 20:5d} / {total >> 20} MB")

    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            PIN_URL, headers=headers, content=body(parts, fields, boundary, report)
        )
    finally:
        if owned:
            client.close()
    if response.status_code >= 400:
        raise SystemExit(f"Pinata refused it ({response.status_code}): {response.text}")
    return response.json()


# -- proving the pin before a name points at it ----------------------------
# Pinning does not publish anything anywhere.

#: Where to prove it. `{cid}` is filled in.
VERIFY_GATEWAY = "https://{cid}.ipfs.dweb.link"

#: The gateway people actually use, checked *after* ENS is moved -- which is
#: the only order available, because eth.limo has no CID gateway at all:
#: https://<cid>.ipfs.eth.limo/ DNS does not resolve
#: https://eth.limo/ipfs/<cid>/ 404 It serves ENS names and nothing else, so
#: its retrieval path cannot be exercised for a CID until the name points at
#: it.
WARM_GATEWAY = "https://curve.eth.limo"

#: Two, not six. Eight parallel probes against eth.limo earned a run of 503s
#: from its rate limiter, which then looked exactly like the failure being
#: investigated.
WARM_WORKERS = 2

#: The gateway saying "not so fast", which is a fact about the request rate
#: and not about the content.
THROTTLE_STATUSES = (429, 503)

#: How long to keep retrying the ones that have not propagated.
VERIFY_DEADLINE = 900.0
VERIFY_INTERVAL = 20.0
VERIFY_TIMEOUT = 45.0
VERIFY_WORKERS = 6

#: A failure faster than this is a decision, not a timeout.
REFUSAL_SECONDS = 3.0

#: Not verified: the token marks, which are fetched only when a pool that
#: uses one is drawn.
LAZY_DIR = "curve"

#: Nor this one, which is fetched *never*. `flet publish` overwrites its own
#: template default with `flet.pyodideUrl="https://cdn.jsdelivr.net/pyodide/
#: v314.0.3/full/pyodide.mjs"`, so Pyodide and its standard library come
#: from jsDelivr and the 15 MB pinned copy is dead weight the app does not
#: know about.
UNFETCHED_DIR = "pyodide"


def boot_files(root: Path) -> list[str]:
    """The part of the build a visitor needs before the app can paint."""
    skip = (LAZY_DIR, UNFETCHED_DIR)
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).parts[0] not in skip
    ]


def classify(status: int | str, seconds: float) -> str:
    """`served`, `throttled`, `refused` or `unfound` -- the difference is the point."""
    if status in (200, 206):
        return "served"
    if status in THROTTLE_STATUSES:
        return "throttled"
    return "refused" if seconds < REFUSAL_SECONDS else "unfound"


def probe(client, url: str, *, whole: bool = False) -> tuple[int | str, float]:
    """Fetch `url` and time it."""
    import httpx

    started = time.monotonic()
    try:
        with client.stream("GET", url, timeout=VERIFY_TIMEOUT) as response:
            for _chunk in response.iter_bytes():
                if not whole:
                    break
            return response.status_code, time.monotonic() - started
    except httpx.HTTPError as exc:
        return type(exc).__name__, time.monotonic() - started


def verify(
    cid: str,
    paths: list[str],
    *,
    gateway: str = VERIFY_GATEWAY,
    deadline: float = VERIFY_DEADLINE,
    interval: float = VERIFY_INTERVAL,
    workers: int = VERIFY_WORKERS,
    whole: bool = False,
    client=None,
    now=time.monotonic,
    sleep=time.sleep,
    on_round=None,
) -> dict[str, tuple[str, int | str, float]]:
    """Poll until every path is retrievable, or until the deadline."""
    import httpx

    base = gateway.format(cid=cid).rstrip("/")
    owned = client is None
    client = client or httpx.Client(follow_redirects=True)
    outstanding = list(paths)
    bad: dict[str, tuple[str, int | str, float]] = {}
    started = now()
    try:
        while outstanding:
            with concurrent.futures.ThreadPoolExecutor(workers) as pool:
                results = list(
                    pool.map(
                        lambda p: (p, *probe(client, f"{base}/{p}", whole=whole)),
                        outstanding,
                    )
                )
            retry = []
            for path, status, seconds in results:
                verdict = classify(status, seconds)
                if verdict == "served":
                    bad.pop(path, None)
                    continue
                bad[path] = (verdict, status, seconds)
                if verdict in ("unfound", "throttled"):
                    retry.append(path)
            if on_round is not None:
                on_round(len(paths) - len(bad), len(paths), now() - started)
            outstanding = retry
            if not outstanding or now() - started >= deadline:
                break
            sleep(interval)
    finally:
        if owned:
            client.close()
    return bad


#: How wide the bar is drawn. Narrow enough to leave room for the counts and
#: the clock on an eighty-column terminal.
BAR_WIDTH = 28


def elapsed_text(seconds: float) -> str:
    """`4m03s`. Minutes because this is measured in them, not in hours."""
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


def progress_bar(served: int, total: int, elapsed: float) -> str:
    """One line of "how much of the pin the network can find yet"."""
    filled = round(BAR_WIDTH * served / total) if total else BAR_WIDTH
    return (
        f"  [{'#' * filled}{'-' * (BAR_WIDTH - filled)}] "
        f"{served:>4}/{total} retrievable   {elapsed_text(elapsed)}"
    )


def progress_reporter(stream=None):
    """Draw the bar in place on a terminal, one line per pass otherwise."""
    stream = stream or sys.stdout
    inline = hasattr(stream, "isatty") and stream.isatty()

    def report(served: int, total: int, elapsed: float) -> None:
        line = progress_bar(served, total, elapsed)
        stream.write(f"\r{line}" if inline else f"{line}\n")
        stream.flush()

    report.inline = inline
    return report


def flet_cli() -> str:
    """The `flet` console script belonging to this interpreter."""
    beside = Path(sys.executable).with_name("flet")
    return str(beside) if beside.is_file() else "flet"


#: What an installed shortcut is called. `tool.flet.product` names the app
#: in the manifest, but the *short* name falls back to the project's own --
#: "flet-curve" -- and only a flag overrides it.
SHORT_NAME = "Curve Finance"


def compile_assets() -> None:
    """`build_assets.py`, so the marks that go up are the marks in the
    submodule as it is pinned right now.
    """
    print("build_assets ...")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_assets.py")],
        cwd=ROOT,
        env=build_env(),
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            "build_assets failed -- is the submodule checked out? "
            "`git submodule update --init`"
        )


def publish() -> None:
    """`flet publish`, so what goes up is what the source says now."""
    print("flet publish ...")
    result = subprocess.run(
        [
            flet_cli(),
            "publish",
            "--app-short-name",
            SHORT_NAME,
            "--route-url-strategy",
            ROUTE_STRATEGY,
        ],
        cwd=ROOT,
        env=build_env(),
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"flet publish failed ({result.returncode})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="pin dist/ as it is")
    parser.add_argument(
        "--dry-run", action="store_true", help="everything except the upload"
    )
    parser.add_argument("--dist", type=Path, default=DIST, help="what to pin")
    parser.add_argument("--name", default="flet-curve", help="the pin's name")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds")
    parser.add_argument(
        "--keep-cdn-copies",
        action="store_true",
        help="pin canvaskit/ and pyodide/ even though a CDN serves them",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="ask the gateways which suffixes they refuse, and print the answer. "
        "Adds nothing to the build.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip proving the pin is retrievable before you point ENS at it",
    )
    parser.add_argument(
        "--verify-only",
        metavar="CID",
        help="skip the build and the upload; just wait on a CID already pinned",
    )
    parser.add_argument(
        "--verify-gateway",
        default=VERIFY_GATEWAY,
        help=f"where to prove it, with {{cid}} filled in (default {VERIFY_GATEWAY})",
    )
    parser.add_argument(
        "--verify-deadline",
        type=float,
        default=VERIFY_DEADLINE,
        help="seconds to keep retrying the ones still propagating",
    )
    parser.add_argument(
        "--warm",
        action="store_true",
        help="after ENS is updated: pull the boot set through the ENS gateway "
        "until it all serves, which both checks it and leaves it cached",
    )
    parser.add_argument(
        "--warm-gateway",
        default=WARM_GATEWAY,
        help=f"the ENS host to warm (default {WARM_GATEWAY})",
    )
    parser.add_argument(
        "--no-warm",
        action="store_true",
        help="stop once the CID is verified, instead of waiting for the ENS "
        "update and warming the gateway afterwards",
    )
    parser.add_argument(
        "--ens-deadline",
        type=float,
        default=ENS_DEADLINE,
        help="seconds to watch for the contenthash to move before giving up",
    )
    options = parser.parse_args()

    if options.warm:
        if not (options.dist / "index.html").is_file():
            raise SystemExit(
                f"{options.dist} holds no index.html, so there is no list of files "
                "to warm. --warm needs the build that was pinned."
            )
        return warm(
            options.warm_gateway.rstrip("/"), boot_files(options.dist), options
        )

    if options.verify_only:
        if not (options.dist / "index.html").is_file():
            raise SystemExit(
                f"{options.dist} holds no index.html, so there is no list of files "
                "to check. --verify-only needs the build the CID was made from."
            )
        show_pin(options.verify_only)
        return wait_until_findable(
            options.verify_only, boot_files(options.dist), options
        )

    if not options.no_build:
        if (swept := clear_bytecode()):
            print(f"swept {len(swept)} bytecode {'directory' if len(swept) == 1 else 'directories'} under src/")
        compile_assets()
        publish()
    dist: Path = options.dist
    if not (dist / "index.html").is_file():
        raise SystemExit(f"{dist} holds no index.html -- run without --no-build")

    strategy = route_strategy(dist / "index.html")
    if strategy != ROUTE_STRATEGY:
        raise SystemExit(
            f"index.html routes on {strategy or 'nothing'!r}, not "
            f"{ROUTE_STRATEGY!r}: every /chain/0x… link would 404 at the "
            "gateway. Rebuild without --no-build, or run `flet publish "
            f"--route-url-strategy {ROUTE_STRATEGY}` -- the pyproject key "
            "alone does not do it; see publish()."
        )
    print(f"index.html: routes on {strategy}, so a gateway can serve them")

    if make_relative(dist / "index.html"):
        print("index.html: base href is relative now, for a gateway sub-path")

    from tools import subset_icons

    icon_font = dist / subset_icons.FONT_RELATIVE
    if icon_font.is_file():
        glyphs, was, now = subset_icons.subset(icon_font, icon_font)
        print(
            f"{subset_icons.FONT_RELATIVE}: {was / 1024:,.0f} KB -> "
            f"{now / 1024:,.0f} KB, {glyphs} glyphs actually drawn"
        )
    if options.keep_cdn_copies:
        print("keeping the CDN-served directories, as asked")
    elif freed := drop_cdn_copies(dist):
        total = sum(size for _name, size in freed)
        detail = ", ".join(f"{name}/ {size / 1e6:.1f} MB" for name, size in freed)
        print(
            f"dropped {detail} -- {total / 1e6:.0f} MB the CDN serves and "
            "nothing fetches from the pin"
        )
    elif not cdn_build(dist / "index.html"):
        print("built --no-cdn, so canvaskit/ and pyodide/ stay: this pin serves them")

    if wrap_package(dist):
        print(f"{PACKAGE_FROM} -> {PACKAGE_TO}: base64, so no gateway reads it as an archive")
    if patch_worker(dist):
        print(f"{WORKER}: taught to unwrap it")
    if options.probe:
        for host in (options.warm_gateway, "https://curve.eth.link"):
            served = probe_suffixes(host.rstrip("/"))
            refused = sorted(s for s, ok in served.items() if not ok)
            print(f"{host} refuses: {' '.join(refused) or 'nothing'}")
            print(f"  and serves: {' '.join(sorted(set(served) - set(refused)))}")

    if found := refused_by_gateway(dist):
        print(
            "note: eth.limo will not serve these, whatever is in them:\n  "
            + "\n  ".join(found)
            + "\n  They are pinned and reachable through other gateways. Nothing "
            "in the\n  published app fetches them today -- Pyodide comes from "
            "jsDelivr, not\n  from the pin -- so this is a heads-up, not a "
            "problem. See REFUSED_SUFFIXES."
        )

    if found := bytecode(dist):
        raise SystemExit(
            "The build carries compiled bytecode:\n  "
            + "\n  ".join(found[:10])
            + (f"\n  ... and {len(found) - 10} more" if len(found) > 10 else "")
            + "\n\nNothing was uploaded. Pyodide cannot load it and a pin is "
            "forever. Rebuild without --no-build, which sweeps it and keeps "
            "the build from writing more."
        )

    jwt = "" if options.dry_run else token()
    if jwt and (found := leaked(dist, jwt)):
        raise SystemExit(
            "The build contains the Pinata key:\n  "
            + "\n  ".join(found)
            + f"\n\nNothing was uploaded. Move it to {SECRETS.name} at the repo root:"
            f" anything under src/ is bundled into the app, and a pin is forever."
        )

    parts = uploads(dist, options.name)
    total = sum(path.stat().st_size for _name, path in parts)
    print(f"{len(parts)} files, {total / (1 << 20):.1f} MB from {dist}")
    if options.dry_run:
        print("--dry-run: stopping before the upload")
        return 0

    answer = pin(parts, fields_for(options.name), jwt, timeout=options.timeout)
    cid = answer.get("IpfsHash", "")
    if not cid:
        raise SystemExit(f"No CID in Pinata's answer: {answer}")

    show_pin(cid, duplicate=bool(answer.get("isDuplicate")))
    if options.no_verify:
        return 0
    paths = boot_files(dist)
    if code := wait_until_findable(cid, paths, options):
        return code
    if options.no_warm:
        return 0

    host = options.warm_gateway.rstrip("/")
    if not wait_for_ens(host, cid, options):
        return 0
    return warm(host, paths, options)


def show_pin(cid: str, *, duplicate: bool = False) -> None:
    """The CID and every way to reach it."""
    print(f"\n  CID  {cid}")
    if duplicate:
        print("       (already pinned -- identical to a previous build)")
    if gateway := str(config().get("gateway", "")).strip():
        print(f"       {gateway.rstrip('/')}/{cid}/")
    print(f"       https://{cid}.ipfs.dweb.link/")
    print(f"       {PUBLIC_GATEWAY}{cid}/")
    print(f"       ipfs://{cid}/")


def wait_until_findable(cid: str, paths: list[str], options) -> int:
    """Watch the pin become retrievable, and say when it is safe to publish."""
    print(f"\nwaiting for the network to find it: {len(paths)} files, via")
    print(f"  {options.verify_gateway.format(cid=cid)}")
    print("  (token marks skipped -- lazily fetched, and there are 6,716)")
    started = time.monotonic()
    report = progress_reporter()
    try:
        bad = verify(
            cid,
            paths,
            gateway=options.verify_gateway,
            deadline=options.verify_deadline,
            on_round=report,
        )
    except KeyboardInterrupt:
        print(
            f"\n\nstopped after {elapsed_text(time.monotonic() - started)}. The pin "
            f"is fine -- this was only the waiting.\n"
            f"  python tools/publish_ipfs.py --verify-only {cid}"
        )
        return 130
    if report.inline:
        print()  # the bar left the cursor on its own line

    if not bad:
        print(
            f"  all {len(paths)} retrievable after "
            f"{elapsed_text(time.monotonic() - started)}"
            " -- safe to point ENS at this CID"
        )
        print(
            "\n  This says the content exists and a CID gateway can serve it.\n"
            "  It does not say eth.limo can: that one has no CID gateway, so\n"
            "  its retrieval path cannot be exercised until the name moves."
        )
        if getattr(options, "no_warm", True):
            print(
                "\n  Once ENS is updated, warm it -- which is also what stops\n"
                "  the first visitor meeting a cold edge:\n\n"
                "    python tools/publish_ipfs.py --warm"
            )
        return 0

    refused = {p: v for p, v in bad.items() if v[0] == "refused"}
    unfound = {p: v for p, v in bad.items() if v[0] == "unfound"}
    for label, group, note in (
        ("refused", refused, "the gateway declines these; waiting will not help"),
        ("not found yet", unfound, "provider records still going out"),
    ):
        if group:
            print(f"\n  {label} -- {note}:")
            for path, (_verdict, status, seconds) in sorted(group.items()):
                print(f"    {status!s:>12}  {seconds:6.2f}s  {path}")
    print(
        "\nThe content is pinned; this is about who can find it. **Do not point"
        " ENS at this CID yet** -- the first request through a gateway is what"
        " gets cached, failures included, so publishing now teaches every"
        " gateway a 404 that outlives the propagation causing it."
    )
    if unfound and not refused:
        print(f"\n  python tools/publish_ipfs.py --verify-only {cid}")
    return 1


#: How often to ask the gateway which CID the name points at now, and how
#: long to keep asking.
ENS_INTERVAL = 30.0
ENS_DEADLINE = 3600.0


def resolved_cid(client, host: str) -> str:
    """Which CID the gateway is serving that name from, or ""."""
    import httpx

    try:
        response = client.get(f"{host}/", timeout=VERIFY_TIMEOUT)
    except httpx.HTTPError:
        return ""
    roots = response.headers.get("x-ipfs-roots", "")
    return roots.split(",")[0].strip()


def wait_for_ens(
    host: str,
    cid: str,
    options,
    *,
    client=None,
    now=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Block until `host` resolves to `cid`. True if it got there."""
    import httpx

    owned = client is None
    client = client or httpx.Client(follow_redirects=True)
    started = now()
    print(f"\nset the ENS contenthash to:\n  ipfs://{cid}")
    print(f"\nwatching {host} for it -- Ctrl-C to stop and warm later")
    try:
        while True:
            live = resolved_cid(client, host)
            if live == cid:
                print(f"  {host} is serving it after {elapsed_text(now() - started)}")
                return True
            waited = elapsed_text(now() - started)
            print(f"  still {live or 'unreadable'}   {waited}", flush=True)
            if now() - started >= options.ens_deadline:
                print(
                    f"\n  gave up after {waited}. The pin is fine and verified;"
                    " only the name has not moved.\n"
                    "  python tools/publish_ipfs.py --warm"
                )
                return False
            sleep(ENS_INTERVAL)
    except KeyboardInterrupt:
        print(
            "\n\nstopped watching. Nothing is lost -- set the contenthash when"
            " you like, then:\n  python tools/publish_ipfs.py --warm"
        )
        return False
    finally:
        if owned:
            client.close()


#: What this stage does *not* cover, said where it is noticed.
NEXT_WARM = (
    "\n  Then warm what a visitor fetches beyond the boot set -- the mark\n"
    "  bundles, and the other gateway:\n\n"
    "      python tools/warm_ipfs.py\n"
)


def warm(host: str, paths: list[str], options) -> int:
    """Pull the boot set through the gateway people use, until it all lands."""
    print(f"\nwarming the gateway people use: {len(paths)} files, via")
    print(f"  {host}")
    print(f"  (whole files, {WARM_WORKERS} at a time -- this is deliberately slow)")
    started = time.monotonic()
    report = progress_reporter()
    try:
        bad = verify(
            "",
            paths,
            gateway=host,
            deadline=options.verify_deadline,
            workers=WARM_WORKERS,
            whole=True,
            on_round=report,
        )
    except KeyboardInterrupt:
        print(
            f"\n\nstopped after {elapsed_text(time.monotonic() - started)}. Whatever "
            "was fetched stays warm;\nthe rest is where it was. Run it again to "
            "carry on."
        )
        return 130
    if report.inline:
        print()

    if not bad:
        print(
            f"  all {len(paths)} served by {host} after "
            f"{elapsed_text(time.monotonic() - started)}"
        )
        print(NEXT_WARM)
        return 0

    print(f"\n  still not served by {host}:")
    for path, (verdict, status, seconds) in sorted(bad.items()):
        print(f"    {verdict:>9}  {status!s:>12}  {seconds:6.2f}s  {path}")
    print(
        "\n  Run it again -- each pass leaves behind what it managed to fetch,\n"
        "  so a file that failed this time is often warm by the next. This is\n"
        "  a mitigation, not a cure: see WARM_GATEWAY."
    )
    print(NEXT_WARM)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
