#!/usr/bin/env python3
"""Pin the web build to IPFS, through Pinata.

    python tools/publish_ipfs.py                # publish, then pin dist/
    python tools/publish_ipfs.py --no-build     # pin the dist/ already there
    python tools/publish_ipfs.py --dry-run      # everything up to the upload

**Where the key goes, and where it must not.** Not in
`src/local_config.toml`. That file is deliberately inside the script
directory because `flet publish` tars that directory into the app, which
is what makes a plain publish come out configured -- and it means
everything under `src/` is shipped to every visitor. Pinning it would then
put it on IPFS, where nobody can offer you a delete button. That file says
in its own header that nothing in it is a secret.

So the Pinata JWT lives in `local_secrets.toml` at the repo *root*, which
is gitignored and outside the tree that gets bundled, or in `PINATA_JWT`
in the environment. The build is searched for it before anything is sent
-- see `leaked` -- because the difference between the safe file and the
unsafe one is a single path component, and the mistake is unrecoverable
rather than embarrassing.

**Why the old endpoint.** Pinata's current upload API,
`uploads.pinata.cloud/v3/files`, does not take a directory, and a website
is a directory. Their own answer to that is `pinFileToIPFS`, so that is
what this posts to: one multipart request, one `file` part per file, each
named `<folder>/<path>` -- which is how the folder is rebuilt on the other
side, and why the CID that comes back is the directory rather than a file.

**Why the body is hand-rolled.** 1,800 files. Handing them all to httpx as
open handles hits the descriptor limit long before it hits the network, so
the parts are streamed one at a time, with the length computed in advance
so the request is not chunked -- and `test_ipfs.py` asserts that the
computed length is exactly what the generator emits, which is the one bug
this shape is prone to.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import re
import secrets
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# Run as `python tools/publish_ipfs.py`, the interpreter puts `tools/` on
# the path and not the repo root, so `from tools import ...` cannot
# resolve -- while the tests, which import this as `tools.publish_ipfs`,
# resolve it fine. That asymmetry shipped a publish script that crashed on
# the one way anybody actually runs it. Same guard as `curve/pool.py`.
if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(ROOT))

#: Outside `src/`, and outside git. Both on purpose -- see the module note.
SECRETS = ROOT / "local_secrets.toml"

PIN_URL = "https://api.pinata.cloud/pinning/pinFileToIPFS"

#: Where to send someone once it is pinned. Not `gateway.pinata.cloud`:
#: that one answers 403 for anything that is HTML ("cannot be served
#: through the pinata public gateway ... utilize a dedicated gateway",
#: ERR_ID:00023), which for a website is every page of it. It serves the
#: js, the wasm and the images quite happily, so the failure looks like a
#: broken pin rather than a policy. A dedicated gateway is the answer, and
#: goes in `local_secrets.toml`; until then this one works.
PUBLIC_GATEWAY = "https://dweb.link/ipfs/"

#: CIDv1 rather than v0: it is case-insensitive base32, which is what a
#: subdomain gateway (`https://<cid>.ipfs.dweb.link`) needs to put the CID
#: in a hostname. v0 hashes only work on path gateways.
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
#: See the README, and `publish()` for why this is a flag rather than the
#: `pyproject.toml` key that looks like it should do the job.
ROUTE_STRATEGY = "hash"

#: index.html names the strategy twice: Flet's template default, and then
#: the value `flet publish` patches in below it. Both are plain assignments
#: in script tags that run in order, so the last one is the one that counts.
_ROUTE_NAMED = re.compile(r'routeUrlStrategy\s*[:=]\s*"(path|hash)"')


def route_strategy(index: Path) -> str:
    """Which strategy the built page will actually use, or "" if unstated."""
    named = _ROUTE_NAMED.findall(index.read_text(encoding="utf-8"))
    return named[-1] if named else ""


def make_relative(index: Path) -> bool:
    """Point the page at its own directory rather than at the site root.

    A gateway serves a pinned site under `/ipfs/<cid>/`, so `href="/"`
    sends the bootstrap, the wasm, canvaskit and the Python tarball to the
    gateway's root, where none of them are -- a blank page and a handful of
    404s. Relative resolves under any prefix, and is identical at a root,
    which is what `tools/serve.py` is.

    Returns whether it changed anything, so a second run is quiet rather
    than wrong. The app's *own* asset URLs need nothing: `ui.assets` builds
    them from the worker's `location`, which is already the right prefix.
    """
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
#:
#: eth.limo and eth.link answer **404** for the archive and 200 for every
#: other file in the same pin -- `main.dart.js`, 9.5 MB across 37 blocks,
#: included. The shell loads, the Python never arrives, and the app sits on
#: "Working..." forever with a single 404 to explain it.
#:
#: Renaming does not get round it, and the measurements say why. A six-byte
#: *text* file called `gateway-probe.tar.gz` is served -- and served
#: labelled `application/gzip`, so the declared type is not what is
#: refused. The real archive under that same name is refused. `.tgz` and
#: `.zip` are refused whatever is inside them, and those are the only other
#: suffixes `python-worker.js` accepts. So the bytes are what is caught, no
#: filename avoids it, and the way out is to stop shipping archive bytes.
#:
#: Base64 inside JSON does that: a legitimate container for binary a script
#: is going to read, text so that nothing can take it for an archive, and
#: still same-origin -- where a gateway URL baked into the page would tie
#: the build to one host, and this has to work on any of them.
#:
#: It costs a third more bytes on 400 KB. `--probe` pins real gzip under
#: several suffixes so a later build can drop this if a cheaper one works.
PACKAGE_FROM = "app.tar.gz"
PACKAGE_TO = "app-package.json"
#: The JSON key, named for what it holds -- so the worker unpacking it as a
#: gzipped tar is reading the file rather than assuming.
PACKAGE_KEY = "gztar"

#: Probes. `probe` under suffixes that differ only in name, and one real
#: gzip archive under suffixes that differ in what a gateway makes of the
#: *content*. Together they separate "this suffix is banned" from "these
#: bytes are", which is the question a rename kept failing to answer.
PROBE_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".gz", ".bin", ".txt")
PROBE_BINARY_SUFFIXES = (".tar.gz", ".wasm", ".png", ".bin")
PROBE_STEM = "gateway-probe"
PROBE_BINARY_STEM = "gateway-probe-binary"


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
#: decision. Anchored on its own source rather than a line number: if Flet
#: rewrites this, the patch fails loudly instead of quietly producing a
#: build whose app package nothing knows how to read.
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


def add_probes(root: Path) -> list[str]:
    """Drop the probe files into the build. Returns their names."""
    names = [f"{PROBE_STEM}{suffix}" for suffix in PROBE_SUFFIXES]
    for name in names:
        (root / name).write_bytes(b"probe\n")
    blob = gzip.compress(b"gateway probe payload\n" * 4096)
    for suffix in PROBE_BINARY_SUFFIXES:
        name = f"{PROBE_BINARY_STEM}{suffix}"
        (root / name).write_bytes(blob)
        names.append(name)
    return names


# -- the check that matters ------------------------------------------------

#: Where a secret could plausibly be readable. The tarball is the app's own
#: source, `local_config.toml` included; the rest is what the page loads.
TEXT_SUFFIXES = {".html", ".js", ".json", ".toml", ".txt", ".mjs", ".py"}
#: `.tgz` as well as `.gz`, because the app package is renamed to `.tgz`
#: before it is pinned -- and it is the one archive in the build that
#: actually contains `src/`, so missing it would gut the check.
ARCHIVE_SUFFIXES = {".gz", ".tgz", ".tar"}
#: Past this a file is canvaskit or a source map, not somewhere a key ends
#: up, and reading 37MB to prove it is time spent on nothing.
MAX_SCAN = 4 << 20


def leaked(root: Path, secret: str) -> list[str]:
    """Files in the build containing `secret`. Empty is the only good answer.

    IPFS has no unpublish. A key that goes up stays up for as long as
    anyone finds it worth pinning, so this runs before the upload rather
    than as a lint afterwards.
    """
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
    return found


def _in_tarball(path: Path, needle: bytes) -> list[str]:
    with tarfile.open(path) as archive:
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
    """How long the body will be, without building it.

    Sent as `Content-Length` so the request is not chunked. It has to agree
    with `body` to the byte: too short truncates the upload, too long hangs
    it until the server gives up.
    """
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
        # With this present httpx streams the generator as-is; without it
        # the request goes out chunked, which this endpoint refuses.
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


# -- putting it together ---------------------------------------------------


def flet_cli() -> str:
    """The `flet` console script belonging to this interpreter.

    Not `python -m flet`: the package has no `__main__`, so that spelling
    fails with "cannot be directly executed" -- and the next thing that
    happens is a pin of whatever `dist/` happened to be holding.
    """
    beside = Path(sys.executable).with_name("flet")
    return str(beside) if beside.is_file() else "flet"


#: What an installed shortcut is called. `tool.flet.product` names the
#: app in the manifest, but the *short* name falls back to the project's
#: own -- "flet-curve" -- and only a flag overrides it. That is the label
#: under the icon on an Android home screen, where "flet-curve" is the
#: repository showing through to somebody who never asked what it was
#: built with.
SHORT_NAME = "Curve Finance"


def publish() -> None:
    """`flet publish`, so what goes up is what the source says now.

    `--route-url-strategy` is passed even though `pyproject.toml` declares
    it, because Flet does not read it there. The option is defined with
    `default="path"` rather than `default=None`, so the value is always
    truthy and the `or get_pyproject("tool.flet.web.route_url_strategy")`
    beside it can never be reached -- a build that trusts the key comes out
    on `path` and 404s every deep link at the gateway. Passing the flag is
    the only thing that works, and `main` checks the result rather than
    trusting this line either.
    """
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
        "--probe",
        action="store_true",
        help="include tiny files under archive-ish suffixes, to find out what "
        "a gateway will not serve",
    )
    options = parser.parse_args()

    if not options.no_build:
        publish()
    dist: Path = options.dist
    if not (dist / "index.html").is_file():
        raise SystemExit(f"{dist} holds no index.html -- run without --no-build")

    # Before anything else about the build: a `path` build looks perfect
    # on any local server -- `tools/serve.py` falls back to index.html --
    # and 404s every deep link on the one thing it is about to be uploaded
    # to. Checked rather than documented, like the leaked-key scan below,
    # because the mistake is invisible until somebody pastes a link.
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

    # 1.26 MB of icon font to draw ten glyphs, and the one file the
    # gateway will not compress. See tools/subset_icons.py.
    from tools import subset_icons

    icon_font = dist / subset_icons.FONT_RELATIVE
    if icon_font.is_file():
        glyphs, was, now = subset_icons.subset(icon_font, icon_font)
        print(
            f"{subset_icons.FONT_RELATIVE}: {was / 1024:,.0f} KB -> "
            f"{now / 1024:,.0f} KB, {glyphs} glyphs actually drawn"
        )
    if wrap_package(dist):
        print(f"{PACKAGE_FROM} -> {PACKAGE_TO}: base64, so no gateway reads it as an archive")
    if patch_worker(dist):
        print(f"{WORKER}: taught to unwrap it")
    if options.probe:
        print("probes: " + ", ".join(add_probes(dist)))

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

    print(f"\n  CID  {cid}")
    if answer.get("isDuplicate"):
        print("       (already pinned -- identical to a previous build)")
    # Yours first, if you have one. A dedicated gateway is the only Pinata
    # host that will serve the HTML.
    if gateway := str(config().get("gateway", "")).strip():
        print(f"       {gateway.rstrip('/')}/{cid}/")
    print(f"       https://{cid}.ipfs.dweb.link/")
    print(f"       {PUBLIC_GATEWAY}{cid}/")
    print(f"       ipfs://{cid}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
