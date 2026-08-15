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
import concurrent.futures
import gzip
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

# Run as `python tools/publish_ipfs.py`, the interpreter puts `tools/` on
# the path and not the repo root, so `from tools import ...` cannot
# resolve -- while the tests, which import this as `tools.publish_ipfs`,
# resolve it fine. That asymmetry shipped a publish script that crashed on
# the one way anybody actually runs it. Same guard as `curve/pool.py`.
if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(ROOT))

#: Outside `src/`, and outside git. Both on purpose -- see the module note.
SECRETS = ROOT / "local_secrets.toml"

#: What `flet publish` tars into the app, and therefore what must be clean
#: before it runs.
SOURCE = ROOT / "src"

#: Compiled bytecode, which goes up unless something stops it.
#:
#: Flet's own tar filter excludes a member whose name *starts with*
#: `__pycache__`, which catches `src/__pycache__` and nothing below it --
#: so `curve/__pycache__/abi.cpython-313.pyc` and forty-seven of its
#: neighbours were pinned in the first published build. They are not
#: merely redundant: Pyodide runs a different Python than the one that
#: wrote them, so nothing can load them even in principle.
BYTECODE = "__pycache__"

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

#: Probes, as a matrix rather than a handful, because the handful is what
#: produced two confident and incompatible readings of the same gateway.
#:
#: Every earlier probe was a few bytes, and the files that actually got
#: refused were hundreds of kilobytes to megabytes -- so "small text
#: served, large archive refused" was read as being about the content when
#: the size moved with it every time. This varies **one thing at a time**:
#:
#:   * `PROBE_CONTENTS` -- text, gzip and zip, at the same size;
#:   * `PROBE_SIZES` -- small and large, for each of those;
#:   * `PROBE_SUFFIXES` -- the archive names and two innocent controls.
#:
#: The large size is past everything seen refused so far, and the text
#: rows are the control: if a 3 MB *text* file named `.zip` is refused,
#: the suffix decides; if a 3 MB zip named `.bin` is refused, the bytes
#: do; if only the large rows are refused whatever they are called, it is
#: a threshold. Those three outcomes are mutually exclusive, which is
#: what the earlier probes could not manage.
#: Large enough to be past everything seen refused so far, the biggest of
#: which was the 2.5 MB `python_stdlib.zip`.
PROBE_LARGE = 3 << 20
PROBE_SMALL = 1 << 10
PROBE_STEM = "gateway-probe"

#: `(content, size, suffix, what a refusal here would prove)`. Five files
#: rather than the full cross product, which came to 42 files and 63 MB --
#: more than half the size of the site, bolted onto a pin whose
#: propagation is already the thing being complained about.
#:
#: Each row holds two variables still and moves one. Read together they
#: are mutually exclusive: at most one of "the suffix", "the bytes" and
#: "the size" survives contact with all five.
PROBE_MATRIX = (
    ("text", PROBE_LARGE, ".zip", "the suffix decides -- innocent bytes, banned name"),
    ("zip", PROBE_LARGE, ".bin", "the bytes decide -- archive under an innocent name"),
    ("zip", PROBE_LARGE, ".whl", "resolves the whl-vs-zip comparison, at one size"),
    ("zip", PROBE_SMALL, ".zip", "the size decides -- banned name and bytes, tiny"),
    ("text", PROBE_LARGE, ".bin", "control: must serve, or the run proves nothing"),
)

#: Archive suffixes an IPFS gateway will not serve. Not an eth.limo
#: quirk -- gateways decline archives generally, presumably so that a pin
#: cannot be used as a file-distribution host, and `.zip`, `.gz` and their
#: relatives are all in scope.
#:
#: **What exactly triggers it is not known, and the obvious readings are
#: confounded.** Everything measured so far varies two things at once:
#:
#:     gateway-probe.tar.gz    6 bytes, text        served
#:     app.tar.gz            ~400 KB, real gzip     refused
#:     packaging-*.whl         96 KB, real zip      served
#:     python_stdlib.zip      2.5 MB, real zip      refused
#:
#: Read down the first pair and the bytes decide; read down the second and
#: the suffix does; read the sizes and neither does -- a threshold explains
#: all four on its own. No experiment yet run separates them, because each
#: pair changed the name *and* the size *and* the content together.
#:
#: So this list is a heads-up about suffixes worth checking, not a theory
#: about what a gateway does, and nothing should be built on a prediction
#: from it. `--probe` is where the experiment belongs: one variable at a
#: time, at a size that matters. Until that has run, the only safe
#: statement is that an archive in a pin may not be reachable, and
#: `verify` is what finds out.
#:
#: The refusal is a fresh 404 in ~0.3s with no `Age` header, which is what
#: distinguishes it from a block that has not propagated yet -- that one is
#: a 504 after ~17s. See `classify`.
#:
#: **This warns rather than stops**, and the reason is worth keeping. The
#: one file it catches today is `pyodide/python_stdlib.zip`, and the
#: published app never asks a gateway for it: `flet publish` overwrites its
#: own template default with
#: `flet.pyodideUrl="https://cdn.jsdelivr.net/pyodide/v314.0.3/full/pyodide.mjs"`,
#: so Pyodide and its standard library come from jsDelivr and the whole
#: pinned `pyodide/` directory is never read. The site loads on
#: curve.eth.link with that file 404ing throughout -- checked in a browser,
#: every request 200 and the pool list populated by Python.
#:
#: So a refusal here is a fact about a file, not about the app, and
#: predicting damage from a filename is what turned an observation into a
#: block that would have stopped a working publish. `verify` measures
#: instead of predicting; this is a heads-up beside it.
REFUSED_SUFFIXES = (".zip", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".7z", ".rar")


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


def probe_payload(kind: str, size: int) -> bytes:
    """`size` bytes of text, gzip or zip -- as close to `size` as each gets.

    Incompressible filler inside the archives, so a 3 MB zip really is 3 MB
    on the wire. Compressible filler would make the large archive rows the
    same size as the small ones and reintroduce the confound this matrix
    exists to remove.
    """
    filler = secrets.token_bytes(size)
    if kind == "text":
        return base64.b64encode(filler)[:size]
    if kind == "gzip":
        return gzip.compress(filler)
    if kind == "zip":
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("payload.bin", filler)
        return buffer.getvalue()
    raise ValueError(kind)


def probe_name(kind: str, size: int, suffix: str) -> str:
    scale = "large" if size >= PROBE_LARGE else "small"
    return f"{PROBE_STEM}-{kind}-{scale}{suffix}"


def add_probes(root: Path) -> list[tuple[str, str]]:
    """Write the probe matrix into the build. Returns `(name, what it tests)`."""
    written = []
    for kind, size, suffix, decides in PROBE_MATRIX:
        name = probe_name(kind, size, suffix)
        (root / name).write_bytes(probe_payload(kind, size))
        written.append((name, decides))
    return written


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

    **The app archive is searched through `app_archive`, not as a file.**
    It is the one member of the build that matters most here -- it is what
    `src/local_config.toml` ends up inside, which is the mistake this whole
    function exists to catch -- and by the time this runs it is no longer a
    tarball at all: `wrap_package` has base64'd it into JSON and deleted
    it. Base64 does not preserve substrings, so scanning that JSON for the
    key matches nothing, and the scan reported clean builds for a shape it
    could not see into.
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
    package = app_archive(root)
    if package is not None:
        name, archive = package
        with archive:
            found += [
                f"{name}:{member}"
                for member in _members_holding(archive, needle)
                # A raw tarball is reached by the loop above as well.
                if f"{name}:{member}" not in found
            ]
    return sorted(found)


def app_archive(root: Path) -> tuple[str, tarfile.TarFile] | None:
    """The app's own archive, whichever shape the build has it in.

    `wrap_package` base64s it into JSON and deletes the tarball, so
    anything that wants to look inside has to know both forms -- and every
    check here runs immediately before the upload, which is *after* the
    wrapping. Reaching for `app.tar.gz` alone finds nothing and says so
    cheerfully.
    """
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
    """`__pycache__` in the build. Empty is the only good answer.

    Checked rather than trusted to the two measures that should have
    prevented it, because `--no-build` skips both: somebody pinning a
    `dist/` they built by hand gets the same protection as somebody who
    let this script build it.
    """
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
    """Files in the build an IPFS gateway will not serve. See REFUSED_SUFFIXES.

    Listed before the upload so that a file which will not be reachable is
    known before it is pinned rather than after somebody reports a page
    that never finishes loading.

    **It does not stop the run.** Today it catches exactly one file,
    `pyodide/python_stdlib.zip`, and the published app never asks a gateway
    for it -- Pyodide is loaded from jsDelivr, so the whole pinned
    `pyodide/` directory goes unread and curve.eth.link loads with that
    file 404ing throughout. The refusal is a fact about a file; whether it
    matters is a fact about the app, and only `verify` and a browser can
    answer that.

    The one archive this build produces itself is handled rather than
    reported: `wrap_package` base64s it into JSON, because for gzip no
    rename is enough.
    """
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
    """The environment the build steps run in.

    Sweeping `__pycache__` first is not enough on its own, and the reason
    is worth stating: `build_assets` imports `ui.assets` to read
    `MARK_PIXELS`, and the interpreter writes that import's bytecode as it
    goes -- after the sweep, before `flet publish` tars the directory. The
    first clean attempt still shipped three files for exactly that reason.
    """
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


# -- proving the pin before a name points at it ----------------------------
#
# Pinning does not publish anything anywhere. Pinata holds the bytes and
# announces provider records into the DHT, and until those records are out
# a gateway asking "who has this block" gets no answer and times out. The
# root CID is one record and goes first, so index.html resolves within
# seconds while individual files 504 for a good while after -- which reads
# exactly like "the site is broken" and is not.
#
# **The reason to gate on it rather than wait a bit** is that the first
# request through a gateway is what gets cached, failures included. Point
# ENS at a CID before its records are out and your own first visit teaches
# that gateway a 404, which then outlives the propagation that caused it.
# So: prove retrievability against a CID URL first, and only then move the
# name that people will actually use.

#: Where to prove it. `{cid}` is filled in. The subdomain form, so this is
#: a different cache key from the ENS hostname the name will resolve to.
#:
#: **This proves the content exists. It does not predict eth.limo.** dweb.link
#: is Cloudflare-fronted and answers `cache-control: public, max-age=29030400,
#: immutable`; it was caught serving with `age=3266`, i.e. from cache, an hour
#: after a build it had fetched once. A pass here means "some gateway holds
#: these bytes", which is worth knowing and is not the question a visitor asks.
#: eth.limo's own nodes do their own provider lookups over a different path,
#: and that path fails intermittently -- see `WARM_GATEWAY`.
VERIFY_GATEWAY = "https://{cid}.ipfs.dweb.link"

#: The gateway people actually use, checked *after* ENS is moved -- which is
#: the only order available, because eth.limo has no CID gateway at all:
#:
#:     https://<cid>.ipfs.eth.limo/   DNS does not resolve
#:     https://eth.limo/ipfs/<cid>/   404
#:
#: It serves ENS names and nothing else, so its retrieval path cannot be
#: exercised for a CID until the name points at it. Hence two stages rather
#: than one: `verify` before, against a CID gateway, and this after.
#:
#: Fetching is also the fix. A block pulled through an edge lands in that
#: edge's store, so the same loop that measures the problem removes it, and
#: the first real visitor gets a warm cache instead of a coin flip. What was
#: measured on the pin that prompted this, minutes apart on one file:
#:
#:     app-package.json   504 unfound (17.4s)  ->  206 served (1.0s)
#:     app-package.json   206 served  ( 1.0s)  ->  504 unfound (17.4s)
#:
#: Not size, either: the 4 KB icon font failed the same way and left every
#: glyph on the page a tofu box.
#:
#: It is a mitigation and not a cure. eth.limo answers `max-age=300` and runs
#: several edges, so a visitor arriving tomorrow on a cold one gets the same
#: coin flip. The durable fix is more peers that can answer -- your own node,
#: or a second pinning service.
WARM_GATEWAY = "https://curve.eth.limo"

#: Two, not six. Eight parallel probes against eth.limo earned a run of 503s
#: from its rate limiter, which then looked exactly like the failure being
#: investigated. Warming is not in a hurry and must not be the reason a
#: gateway starts refusing.
WARM_WORKERS = 2

#: The gateway saying "not so fast", which is a fact about the request rate
#: and not about the content. Retried like `unfound` rather than recorded as
#: a refusal -- a 503 arrives in well under `REFUSAL_SECONDS` and would
#: otherwise be filed as a permanent decision and never tried again.
THROTTLE_STATUSES = (429, 503)

#: How long to keep retrying the ones that have not propagated. Fifteen
#: minutes is well past what a few thousand blocks has taken in practice
#: and short enough to sit and watch.
VERIFY_DEADLINE = 900.0
VERIFY_INTERVAL = 20.0
VERIFY_TIMEOUT = 45.0
VERIFY_WORKERS = 6

#: A failure faster than this is a decision, not a timeout. A gateway that
#: cannot find a block spends its whole retrieval budget first -- ~17s in
#: the case that prompted this -- where one that refuses to serve the file
#: answers in a third of a second. Waiting fixes the first and never the
#: second, so they must not be reported as the same thing.
REFUSAL_SECONDS = 3.0

#: Not verified: the token marks, which are fetched only when a pool that
#: uses one is drawn. 6,716 files against the 92 a visitor needs to boot,
#: and hammering a public gateway for art nobody has asked for yet is not
#: a check, it is an imposition.
LAZY_DIR = "curve"

#: Nor this one, which is fetched *never*. `flet publish` overwrites its own
#: template default with
#: `flet.pyodideUrl="https://cdn.jsdelivr.net/pyodide/v314.0.3/full/pyodide.mjs"`,
#: so Pyodide and its standard library come from jsDelivr and the 15 MB
#: pinned copy is dead weight the app does not know about.
#:
#: Measured rather than assumed, twice: a browser load of curve.eth.limo
#: shows 124 requests, canvaskit from gstatic and rive from jsDelivr, and
#: not one request under `pyodide/`. The app renders the pool list with live
#: data while these files 404.
#:
#: Left in the list they are permanent noise -- `python_stdlib.zip` is an
#: archive and gateways decline those outright, `package.json` answered 502
#: on four attempts in a row -- so every run would end in a failure report
#: about files nothing reads, which is how you teach someone to stop reading
#: the report. `refused_by_gateway` still names them.
UNFETCHED_DIR = "pyodide"


def boot_files(root: Path) -> list[str]:
    """The part of the build a visitor needs before the app can paint.

    Derived from the build rather than listed here, for the reason
    `build_assets` reads the submodule's directory rather than a table: a
    list kept in the tool goes stale silently, and the failure mode is a
    file nobody checked being the one that does not serve.
    """
    skip = (LAZY_DIR, UNFETCHED_DIR)
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).parts[0] not in skip
    ]


def classify(status: int | str, seconds: float) -> str:
    """`served`, `throttled`, `refused` or `unfound` -- the difference is the point.

    `throttled` is separated from `refused` because both are fast and only
    one of them is about the file. A rate-limited 503 lands in a fraction of
    a second, which is the signature this used to read as a permanent
    decision, and it would then never be retried -- turning our own request
    rate into a report that the gateway declines to serve the app.
    """
    if status in (200, 206):
        return "served"
    if status in THROTTLE_STATUSES:
        return "throttled"
    return "refused" if seconds < REFUSAL_SECONDS else "unfound"


def probe(client, url: str, *, whole: bool = False) -> tuple[int | str, float]:
    """Fetch `url` and time it.

    By default streamed and abandoned after the first chunk: proving a 9 MB
    file is retrievable does not require moving 9 MB, and this runs against
    somebody else's gateway.

    `whole=True` reads to the end, which is what warming needs and checking
    does not. The first chunk is the first block; leaving after it warms one
    block of the thirty-seven in `main.dart.js` and reports the file done.
    """
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
    """Poll until every path is retrievable, or until the deadline.

    Returns only what is still wrong, keyed by path. Anything classified
    `refused` is returned immediately and never retried: a gateway that
    declines to serve a suffix will decline for the rest of the day.
    `throttled` is retried like `unfound` -- it is our request rate talking,
    not the gateway's opinion of the file.

    `whole=True` pulls each file to the end rather than sampling its first
    block, which is what makes this warm a gateway as well as measure it.
    `gateway` needs no `{cid}`: an ENS host is a fixed base and formats to
    itself, which is how the post-ENS stage reuses all of this.

    `on_round(served, total, elapsed)` after each pass, so the caller owns
    the display -- this has to be usable from a test without a terminal,
    and the bar has to be drawable without reaching into the polling.

    **Interrupting is a supported way to leave.** The pin exists whatever
    happens here; the caller catches `KeyboardInterrupt` and keeps the CID
    on screen, and `--verify-only` picks the waiting back up later.
    """
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


#: How wide the bar is drawn. Narrow enough to leave room for the counts
#: and the clock on an eighty-column terminal.
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
    """Draw the bar in place on a terminal, one line per pass otherwise.

    A `\\r` bar piped into a file is thousands of overwritten lines and no
    way to read what happened, so the redirected case gets plain lines --
    which is also what a CI log wants.
    """
    stream = stream or sys.stdout
    inline = hasattr(stream, "isatty") and stream.isatty()

    def report(served: int, total: int, elapsed: float) -> None:
        line = progress_bar(served, total, elapsed)
        stream.write(f"\r{line}" if inline else f"{line}\n")
        stream.flush()

    #: Whether the last line still needs closing. A `\r` bar leaves the
    #: cursor on it; plain lines have already ended.
    report.inline = inline
    return report


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


def compile_assets() -> None:
    """`build_assets.py`, so the marks that go up are the marks in the
    submodule as it is pinned right now.

    Run here rather than left to whoever is publishing, because its
    output is gitignored -- correctly, it is generated -- and so a stale
    `src/assets/curve` is invisible in `git status`. It cannot be
    forgotten if nobody has to remember it. Every asset change this
    session needed a manual rebuild before it meant anything, and there
    was a window each time where a pin could have carried the old art.
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
        "--probe",
        action="store_true",
        help="include tiny files under archive-ish suffixes, to find out what "
        "a gateway will not serve",
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

    # Warming needs no CID and no key: it asks the ENS name for the files the
    # build says a visitor needs, which is the same question a visitor asks.
    # It reads the list from `dist/` for the same reason `--verify-only` does
    # -- a CID names exactly one tree, and this is that tree.
    if options.warm:
        if not (options.dist / "index.html").is_file():
            raise SystemExit(
                f"{options.dist} holds no index.html, so there is no list of files "
                "to warm. --warm needs the build that was pinned."
            )
        return warm(
            options.warm_gateway.rstrip("/"), boot_files(options.dist), options
        )

    # Resuming a wait. Nothing is built, nothing is sent, and no key is
    # needed -- the pin already exists and this only asks the network
    # whether it can find it yet. What to ask for still comes from the
    # build, which is the same build: a CID names exactly one tree.
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
        # Before either step, because both import from `src/` and
        # `flet publish` tars whatever is in there -- see `BYTECODE`.
        if (swept := clear_bytecode()):
            print(f"swept {len(swept)} bytecode {'directory' if len(swept) == 1 else 'directories'} under src/")
        # The marks first: `flet publish` tars `src/` into the app, so
        # anything compiled after it would not be in the build.
        compile_assets()
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
        print("probes -- fetch each of these once the pin is retrievable:")
        for name, decides in add_probes(dist):
            print(f"  {name:<34} refused => {decides}")

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

    # The CID first, and unconditionally. It is the one thing worth having
    # from this run: the upload succeeded, the content is pinned, and
    # everything below is about *when* the rest of the world can find it.
    # Holding it back behind a check that can legitimately take a quarter
    # of an hour means a slow network reads as a failed publish.
    show_pin(cid, duplicate=bool(answer.get("isDuplicate")))
    if options.no_verify:
        return 0
    paths = boot_files(dist)
    if code := wait_until_findable(cid, paths, options):
        return code
    if options.no_warm:
        return 0

    # One run, three stages, because the middle one is yours. The script
    # cannot move the name -- that is a wallet signature -- but it can watch
    # for it, and waiting here is what keeps the warming attached to the
    # publish that needs it rather than to a command you have to remember an
    # hour later.
    host = options.warm_gateway.rstrip("/")
    if not wait_for_ens(host, cid, options):
        return 0
    return warm(host, paths, options)


def show_pin(cid: str, *, duplicate: bool = False) -> None:
    """The CID and every way to reach it."""
    print(f"\n  CID  {cid}")
    if duplicate:
        print("       (already pinned -- identical to a previous build)")
    # Yours first, if you have one. A dedicated gateway is the only Pinata
    # host that will serve the HTML.
    if gateway := str(config().get("gateway", "")).strip():
        print(f"       {gateway.rstrip('/')}/{cid}/")
    print(f"       https://{cid}.ipfs.dweb.link/")
    print(f"       {PUBLIC_GATEWAY}{cid}/")
    print(f"       ipfs://{cid}/")


def wait_until_findable(cid: str, paths: list[str], options) -> int:
    """Watch the pin become retrievable, and say when it is safe to publish.

    Separate from the upload because it is a separate question with a
    separate answer. The pin is done; this is the network catching up, it
    is slow, and it is interruptible -- Ctrl-C leaves the CID standing and
    `--verify-only` resumes.
    """
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
#: long to keep asking. An hour because the wait is a wallet signature and a
#: block confirmation, not a network condition -- and because leaving it
#: running costs one request every half minute.
ENS_INTERVAL = 30.0
ENS_DEADLINE = 3600.0


def resolved_cid(client, host: str) -> str:
    """Which CID the gateway is serving that name from, or "".

    Every eth.limo response carries it, which is how ENS was ruled out as
    the cause of a bad load in the first place:

        x-ipfs-roots: bafybeig4zzt5yofgwdpbval6p3osa3kbf4tidnnxjttjvbtwsyuf3xmlkq

    On a subpath the header lists the root and then each node down to the
    file, so the root is the first entry. Asking for `/` keeps it to one.
    """
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
    """Block until `host` resolves to `cid`. True if it got there.

    The one step of a publish this script does not perform -- moving the
    contenthash is a wallet signature -- so it watches for it instead of
    asking you to come back afterwards. Warming before the name moves would
    faithfully warm the *previous* build, which is why this is a gate and
    not a pause.

    **Ctrl-C is a supported way to leave**, as everywhere else here: the pin
    is done and verified, and `--warm` picks up whenever you are ready.
    """
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


def warm(host: str, paths: list[str], options) -> int:
    """Pull the boot set through the gateway people use, until it all lands.

    Runs *after* ENS is updated, and is the only stage that touches the
    retrieval path a visitor gets -- see `WARM_GATEWAY` for why that order
    is forced rather than chosen. Measuring and fixing are the same act
    here: a block fetched through an edge stays in that edge's store.

    Nothing about it is fast. Two workers and whole files, because the
    alternative was teaching eth.limo's rate limiter to answer 503 and then
    reading those 503s back as a broken pin.
    """
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
        return 0

    print(f"\n  still not served by {host}:")
    for path, (verdict, status, seconds) in sorted(bad.items()):
        print(f"    {verdict:>9}  {status!s:>12}  {seconds:6.2f}s  {path}")
    print(
        "\n  Run it again -- each pass leaves behind what it managed to fetch,\n"
        "  so a file that failed this time is often warm by the next. This is\n"
        "  a mitigation, not a cure: see WARM_GATEWAY."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
