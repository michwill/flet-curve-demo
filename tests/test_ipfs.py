"""Pinning the web build, without a Pinata account or a network.

Two things here can fail in a way that costs something real rather than a
redraw. A hand-rolled multipart body whose declared length disagrees with
what it emits truncates or hangs the upload -- 108 MB in, and no way to
tell which half arrived. And a build that carries the API key gets pinned
with it, which is not a mistake anybody can take back: IPFS has no
unpublish, and the key is a live credential until it is rotated.

So those two are tested against real bytes and a real tarball, and the
rest of the module is checked for the shape Pinata's directory upload
needs -- one `file` part per file, each named `<folder>/<path>`, which is
the only reason the CID that comes back is a directory.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import tarfile
import textwrap
from pathlib import Path

import httpx
import pytest

from tools import publish_ipfs as ipfs


def build(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "dist"
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# -- the body ---------------------------------------------------------------


def test_the_declared_length_is_exactly_what_goes_out(tmp_path: Path) -> None:
    """The bug this shape invites: a body that does not match its own
    Content-Length. Short truncates the upload, long hangs it."""
    root = build(tmp_path, {"index.html": "<html>", "a/b.js": "x" * 5000, "e.txt": ""})
    parts = ipfs.uploads(root, "site")
    fields = ipfs.fields_for("site")

    declared = ipfs.content_length(parts, fields, "BOUND")
    emitted = sum(len(chunk) for chunk in ipfs.body(parts, fields, "BOUND"))

    assert declared == emitted


def test_a_file_part_is_named_for_its_place_in_the_folder(tmp_path: Path) -> None:
    """This is what makes it a directory on the other side rather than
    1,800 loose files: the name carries the path."""
    root = build(tmp_path, {"index.html": "<html>", "curve/tokens/a.png": "img"})

    named = dict(ipfs.uploads(root, "flet-curve"))

    assert set(named) == {"flet-curve/index.html", "flet-curve/curve/tokens/a.png"}


def test_every_file_goes_up_under_the_same_field_name(tmp_path: Path) -> None:
    root = build(tmp_path, {"a.js": "1", "b/c.js": "2"})
    parts = ipfs.uploads(root, "site")

    raw = b"".join(ipfs.body(parts, {}, "BOUND")).decode()

    assert raw.count('name="file"') == 2
    assert raw.endswith("--BOUND--\r\n")


def test_the_options_ask_for_a_cid_a_subdomain_gateway_can_hold() -> None:
    """v0 hashes are base58 and case-sensitive, so they cannot go in a
    hostname -- `https://<cid>.ipfs.dweb.link` needs v1."""
    options = json.loads(ipfs.fields_for("x")["pinataOptions"])
    assert options["cidVersion"] == 1


# -- building first ---------------------------------------------------------


def test_the_build_runs_the_console_script_not_dash_m() -> None:
    """`python -m flet` raises "cannot be directly executed" -- the package
    has no `__main__`. The build then fails and the next step would pin
    whatever `dist/` was already holding."""
    command = ipfs.flet_cli()

    assert command.endswith("flet")
    assert "-m" not in command


# -- the app package --------------------------------------------------------


def test_the_package_is_wrapped_as_text_and_the_page_follows(tmp_path: Path) -> None:
    """eth.limo serves a six-byte text file named `.tar.gz` -- labelled
    application/gzip, even -- and refuses the real archive under the same
    name. The bytes are what is caught, so the bytes stop being an
    archive."""
    root = build(
        tmp_path,
        {
            "index.html": '<head><base href="./"></head>'
            '<script>appPackageUrl: "app.tar.gz"</script>',
        },
    )
    (root / "app.tar.gz").write_bytes(b"\x1f\x8b pretend gzip")

    assert ipfs.wrap_package(root) is True
    assert not (root / "app.tar.gz").exists()
    assert '"app-package.json"' in (root / "index.html").read_text(encoding="utf-8")

    payload = json.loads((root / ipfs.PACKAGE_TO).read_text(encoding="utf-8"))
    assert base64.b64decode(payload[ipfs.PACKAGE_KEY]) == b"\x1f\x8b pretend gzip"


def test_the_worker_can_read_what_the_wrapper_writes(tmp_path: Path) -> None:
    """The contract between the two halves, run end to end: a real gzipped
    tar, wrapped here, unwrapped by the same three steps the patched worker
    performs. A key or format that disagreed would otherwise surface as an
    app that downloads its Python and cannot open it."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text('appPackageUrl: "app.tar.gz"', encoding="utf-8")
    with tarfile.open(root / "app.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("main.py")
        member.size = len(b"print('hi')\n")
        archive.addfile(member, io.BytesIO(b"print('hi')\n"))

    ipfs.wrap_package(root)

    # Exactly what WORKER_PATCH does, in the same order.
    blob = json.loads((root / ipfs.PACKAGE_TO).read_text())[ipfs.PACKAGE_KEY]
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(blob))) as archive:
        assert archive.getnames() == ["main.py"]
        assert archive.extractfile("main.py").read() == b"print('hi')\n"


def test_wrapping_a_build_that_is_already_wrapped_does_nothing(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "x", ipfs.PACKAGE_TO: "{}"})
    assert ipfs.wrap_package(root) is False


def test_the_archive_is_not_wrapped_without_the_line_that_points_at_it(
    tmp_path: Path,
) -> None:
    """Wrapping one and not the other is a build that fetches a file that
    is not there -- the same blank page, with no 404 to explain it."""
    root = build(tmp_path, {"index.html": "<head>nothing here</head>", "app.tar.gz": "x"})
    with pytest.raises(SystemExit, match="appPackageUrl"):
        ipfs.wrap_package(root)
    assert (root / "app.tar.gz").is_file()


# -- the worker patch -------------------------------------------------------


def worker_source() -> str:
    """The two lines of Flet's worker that the patch anchors on."""
    return (
        "            from urllib.parse import urlparse\n"
        + ipfs.WORKER_ANCHOR
        + '\n                _archive_format = "zip"\n'
        + ipfs.WORKER_UNPACK
        + "\n"
    )


def test_the_worker_learns_to_unwrap_and_skips_the_old_path(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    root.mkdir()
    (root / ipfs.WORKER).write_text(worker_source(), encoding="utf-8")

    assert ipfs.patch_worker(root) is True
    patched = (root / ipfs.WORKER).read_text(encoding="utf-8")

    assert '.endswith(".json")' in patched
    # The archive path is still there for a build served from anywhere
    # else, but it no longer runs for the wrapped one.
    assert "if _archive_format:" in patched
    assert ipfs.WORKER_UNPACK.strip() in patched


def test_patching_a_patched_worker_does_nothing(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    root.mkdir()
    (root / ipfs.WORKER).write_text(worker_source(), encoding="utf-8")
    ipfs.patch_worker(root)
    assert ipfs.patch_worker(root) is False


def test_a_worker_that_has_changed_shape_stops_the_run(tmp_path: Path) -> None:
    """Flet regenerates this file. Patching it blind would leave a build
    that downloads its Python and quietly ignores it."""
    root = tmp_path / "dist"
    root.mkdir()
    (root / ipfs.WORKER).write_text("something else entirely", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed shape"):
        ipfs.patch_worker(root)


def test_the_python_injected_into_the_worker_parses() -> None:
    """It is Python inside a JavaScript string inside a generated file --
    nothing type-checks it, and a syntax error there is a blank page after
    a pin, an ENS update and a wait."""
    branch = "\n".join(ipfs.WORKER_PATCH.splitlines()[:-1])  # drop the trailing elif
    body = textwrap.indent(textwrap.dedent(branch), "    ")
    compile(
        f"async def _run(response, _archive_path, _archive_format):\n{body}\n    pass",
        "<worker patch>",
        "exec",
    )


def test_probes_ask_both_questions(tmp_path: Path) -> None:
    """Text under each suffix says which *names* are refused; one real
    archive under several suffixes says whether the *bytes* are."""
    root = build(tmp_path, {"index.html": "x"})
    names = ipfs.add_probes(root)

    assert f"{ipfs.PROBE_STEM}.tar.gz" in names
    assert f"{ipfs.PROBE_BINARY_STEM}.wasm" in names
    assert all((root / name).is_file() for name in names)
    # The binary ones are a real gzip member, or they answer nothing.
    blob = (root / f"{ipfs.PROBE_BINARY_STEM}.wasm").read_bytes()
    assert blob[:2] == b"\x1f\x8b"
    assert gzip.decompress(blob).startswith(b"gateway probe payload")


# -- the check before the upload -------------------------------------------


def test_a_key_in_the_build_is_found_in_a_plain_file(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "config.json": '{"jwt":"SEKRIT"}'})
    assert ipfs.leaked(root, "SEKRIT") == ["config.json"]


def test_a_key_inside_the_python_tarball_is_found_too(tmp_path: Path) -> None:
    """Which is the case that would actually happen: `flet publish` tars
    `src/` into `app.tar.gz`, and `src/local_config.toml` is in there."""
    root = build(tmp_path, {"index.html": "<html>"})
    buffer = io.BytesIO(b'jwt = "SEKRIT"\n')
    with tarfile.open(root / "app.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("src/local_config.toml")
        info.size = len(buffer.getvalue())
        archive.addfile(info, buffer)

    assert ipfs.leaked(root, "SEKRIT") == ["app.tar.gz:src/local_config.toml"]


def test_the_renamed_package_is_still_searched(tmp_path: Path) -> None:
    """The rename to `.tgz` happens before the scan, and `app.tgz` is the
    one archive in the build that holds `src/` -- looking only for `.gz`
    would leave the check passing over the file it exists for."""
    root = build(tmp_path, {"index.html": "<html>"})
    buffer = io.BytesIO(b'jwt = "SEKRIT"\n')
    with tarfile.open(root / "app.tgz", "w:gz") as archive:
        info = tarfile.TarInfo("src/local_config.toml")
        info.size = len(buffer.getvalue())
        archive.addfile(info, buffer)

    assert ipfs.leaked(root, "SEKRIT") == ["app.tgz:src/local_config.toml"]


def test_a_key_is_still_found_once_the_package_is_wrapped(tmp_path: Path) -> None:
    """The shape the build is in when the scan actually runs.

    `wrap_package` base64s the archive and deletes it, and `main` does that
    *before* this check -- so the one file `src/local_config.toml` ends up
    in was, for a while, the one file the scan could not read. Base64 does
    not preserve substrings, so searching the JSON found nothing and the
    build was pronounced clean.
    """
    root = build(
        tmp_path,
        {"index.html": f'<script>appPackageUrl: "{ipfs.PACKAGE_FROM}"</script>'},
    )
    buffer = io.BytesIO(b'jwt = "SEKRIT"\n')
    with tarfile.open(root / ipfs.PACKAGE_FROM, "w:gz") as archive:
        info = tarfile.TarInfo("src/local_config.toml")
        info.size = len(buffer.getvalue())
        archive.addfile(info, buffer)
    assert ipfs.wrap_package(root) is True

    assert ipfs.leaked(root, "SEKRIT") == [
        f"{ipfs.PACKAGE_TO}:src/local_config.toml"
    ]


def test_a_key_in_the_wrapped_package_is_named_once(tmp_path: Path) -> None:
    """A raw tarball is reached by the file walk and by the archive pass,
    and one leak reported twice reads as two."""
    root = build(tmp_path, {"index.html": "<html>"})
    buffer = io.BytesIO(b'jwt = "SEKRIT"\n')
    with tarfile.open(root / ipfs.PACKAGE_FROM, "w:gz") as archive:
        info = tarfile.TarInfo("src/local_config.toml")
        info.size = len(buffer.getvalue())
        archive.addfile(info, buffer)

    assert ipfs.leaked(root, "SEKRIT") == [
        f"{ipfs.PACKAGE_FROM}:src/local_config.toml"
    ]


def test_a_clean_build_reports_nothing(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "main.dart.js": "console.log(1)"})
    assert ipfs.leaked(root, "SEKRIT") == []


# -- compiled bytecode ------------------------------------------------------


def _packaged(root: Path, members: dict[str, bytes]) -> None:
    """Write `app.tar.gz` holding exactly these members."""
    with tarfile.open(root / ipfs.PACKAGE_FROM, "w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_bytecode_below_the_top_level_is_found(tmp_path: Path) -> None:
    """The case that actually shipped. Flet's own tar filter drops a member
    whose name *starts with* `__pycache__`, so `src/__pycache__` never got
    in and `curve/__pycache__/...` always did -- forty-eight files of it."""
    root = build(tmp_path, {"index.html": "<html>"})
    _packaged(
        root,
        {
            "curve/abi.py": b"x = 1\n",
            "curve/__pycache__/abi.cpython-313.pyc": b"\x00\x00",
            "ui/__pycache__/assets.cpython-313.pyc": b"\x00\x00",
        },
    )

    assert ipfs.bytecode(root) == [
        f"{ipfs.PACKAGE_FROM}:curve/__pycache__/abi.cpython-313.pyc",
        f"{ipfs.PACKAGE_FROM}:ui/__pycache__/assets.cpython-313.pyc",
    ]


def test_bytecode_is_found_after_the_package_is_wrapped(tmp_path: Path) -> None:
    """Which is the shape it is in when the check runs: `wrap_package` has
    already base64'd the archive and deleted it. Looking for `app.tar.gz`
    at that point finds nothing and says the build is clean."""
    root = build(
        tmp_path,
        {"index.html": f'<script>appPackageUrl: "{ipfs.PACKAGE_FROM}"</script>'},
    )
    _packaged(root, {"curve/__pycache__/abi.cpython-313.pyc": b"\x00\x00"})
    assert ipfs.wrap_package(root) is True
    assert not (root / ipfs.PACKAGE_FROM).exists()

    assert ipfs.bytecode(root) == [
        f"{ipfs.PACKAGE_TO}:curve/__pycache__/abi.cpython-313.pyc"
    ]


def test_loose_bytecode_in_the_build_counts_too(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "__pycache__/x.pyc": "junk"})
    assert ipfs.bytecode(root) == ["__pycache__/x.pyc"]


def test_a_build_with_no_bytecode_reports_nothing(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>"})
    _packaged(root, {"curve/abi.py": b"x = 1\n"})
    assert ipfs.bytecode(root) == []


def test_the_build_runs_with_bytecode_writing_off() -> None:
    """Sweeping first is not enough: `build_assets` imports `ui.assets` for
    `MARK_PIXELS`, and the import writes its own bytecode -- after the
    sweep and before `flet publish` tars the directory."""
    assert ipfs.build_env()["PYTHONDONTWRITEBYTECODE"] == "1"


# -- the base href ----------------------------------------------------------


def test_the_page_is_pointed_at_its_own_directory(tmp_path: Path) -> None:
    """A gateway serves the site under `/ipfs/<cid>/`, where an absolute
    base sends every script to the gateway's root and nothing loads."""
    index = tmp_path / "index.html"
    index.write_text('<head><base href="/">\n</head>', encoding="utf-8")

    assert ipfs.make_relative(index) is True
    assert '<base href="./">' in index.read_text(encoding="utf-8")


def test_making_it_relative_twice_changes_nothing(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    index.write_text('<head><base href="./"></head>', encoding="utf-8")
    assert ipfs.make_relative(index) is False


def test_an_index_with_no_base_tag_stops_the_run(tmp_path: Path) -> None:
    """Rather than pinning a build that cannot load. It would mean Flet's
    index patcher changed, which is worth being told about."""
    index = tmp_path / "index.html"
    index.write_text("<head></head>", encoding="utf-8")
    with pytest.raises(SystemExit, match="patcher"):
        ipfs.make_relative(index)


# -- credentials ------------------------------------------------------------


def test_the_environment_wins_over_the_file(monkeypatch) -> None:
    """So CI needs no file, and a shell can override one for a test pin."""
    monkeypatch.setenv("PINATA_JWT", "from-env")
    assert ipfs.token({"jwt": "from-file"}) == "from-env"


def test_the_file_is_used_when_the_environment_is_empty(monkeypatch) -> None:
    monkeypatch.delenv("PINATA_JWT", raising=False)
    assert ipfs.token({"jwt": "from-file"}) == "from-file"


def test_no_key_anywhere_says_where_to_put_one(monkeypatch) -> None:
    monkeypatch.delenv("PINATA_JWT", raising=False)
    with pytest.raises(SystemExit, match=r"local_secrets\.toml"):
        ipfs.token({})


# -- the request ------------------------------------------------------------


def test_the_upload_is_authorised_declared_and_read_back(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "a.js": "x" * 100})
    parts = ipfs.uploads(root, "site")
    fields = ipfs.fields_for("site")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["length"] = request.headers.get("content-length")
        seen["chunked"] = request.headers.get("transfer-encoding")
        seen["body"] = request.read()
        return httpx.Response(200, json={"IpfsHash": "bafyOK", "PinSize": 7})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    answer = ipfs.pin(parts, fields, "the-jwt", timeout=5, client=client)

    assert answer["IpfsHash"] == "bafyOK"
    assert seen["url"] == ipfs.PIN_URL
    assert seen["auth"] == "Bearer the-jwt"
    # Declared, not chunked -- and the declaration held all the way to the
    # wire, which is the whole point of computing it up front.
    assert seen["chunked"] is None
    assert int(seen["length"]) == len(seen["body"])


def test_a_refusal_is_reported_rather_than_returned(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>"})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="UNAUTHORIZED")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SystemExit, match=r"401.*UNAUTHORIZED"):
        ipfs.pin(ipfs.uploads(root, "s"), {}, "bad", timeout=5, client=client)


# -- routes that survive a gateway ------------------------------------------


def test_routes_live_in_the_fragment() -> None:
    """A gateway is a filesystem, not a server.

    It resolves the request path inside the published directory and 404s
    when there is no such file, and there is no file called `ethereum`.
    So `/ethereum/0xC09e…` -- the kind of link this app exists to hand out
    -- died at the gateway while working against any local server, which
    falls back to index.html the way a normal SPA host does.

    A fragment is never sent to the server: the gateway is asked for `/`,
    serves index.html, and the app reads the route from the fragment. It
    needs nothing from the gateway, so it works the same on a path
    gateway, a subdomain gateway and an ENS name through eth.limo.

    Declared in `pyproject.toml` because that is the documented way to say
    it -- but Flet does not read it there, which is what the next two
    tests are about, so nothing may rely on this key alone.
    """
    import tomllib

    root = Path(ipfs.__file__).resolve().parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text())
    web = config["tool"]["flet"]["web"]

    assert web["route_url_strategy"] == "hash"


def test_the_strategy_is_passed_as_a_flag_because_the_key_is_not_read() -> None:
    """`flet publish` defines `--route-url-strategy` with `default="path"`,
    so `options.route_url_strategy` is always truthy and the
    `or get_pyproject(...)` beside it is unreachable. A build that trusts
    `pyproject.toml` comes out on `path` and 404s every deep link."""
    source = Path(ipfs.__file__).read_text()

    assert "--route-url-strategy" in source
    assert ipfs.ROUTE_STRATEGY == "hash"


def test_the_effective_strategy_is_the_last_one_named(tmp_path) -> None:
    """index.html names it twice -- Flet's template default, then the value
    `flet publish` patches in below. Both are assignments in script tags
    that run in order, so the later one is what the page uses. Reading the
    first would pass a build that is about to 404 everywhere."""
    index = tmp_path / "index.html"
    index.write_text(
        '<script>var flet = { routeUrlStrategy: "path" }</script>\n'
        '<script>flet.routeUrlStrategy="hash";</script>\n'
    )
    assert ipfs.route_strategy(index) == "hash"

    index.write_text('<script>var flet = { routeUrlStrategy: "path" }</script>')
    assert ipfs.route_strategy(index) == "path"

    index.write_text("<script>nothing to see</script>")
    assert ipfs.route_strategy(index) == ""


def test_the_base_href_stays_relative() -> None:
    """The other half of the same problem, and the reason `_redirects` is
    not the answer: a gateway serves this under `/ipfs/<cid>/`, so the
    assets are found relatively. Serving index.html at a deep path instead
    would resolve them against that path and 404 every one."""
    assert ipfs.BASE_RELATIVE == '<base href="./">'
    assert ipfs.BASE_ABSOLUTE == '<base href="/">'


# -- running it the way anybody runs it -------------------------------------


def test_the_script_runs_as_a_script(tmp_path) -> None:
    """`python tools/publish_ipfs.py`, not `import tools.publish_ipfs`.

    Those two put different things on `sys.path`: as a script the
    interpreter contributes `tools/` and not the repo root, so a
    `from tools import ...` inside resolves under pytest and crashes for
    the person publishing. It did, with `ModuleNotFoundError: No module
    named 'tools'`, and every test in this file passed while it did --
    because every test in this file imports the module rather than running
    it. So this one runs it.
    """
    import io
    import shutil
    import subprocess
    import sys
    import tarfile

    import flet_web

    root = Path(ipfs.__file__).resolve().parent.parent
    web = Path(flet_web.__file__).parent / "web"

    # Enough of a build for the script to walk the whole way through: the
    # page it rewrites, the font it cuts, the worker it patches, and the
    # archive it wraps.
    dist = tmp_path / "dist"
    (dist / "assets/fonts").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><head><base href="/">'
        '<script>flet.routeUrlStrategy="hash";'
        'appPackageUrl: "app.tar.gz"</script></head></html>',
        encoding="utf-8",
    )
    shutil.copy(web / "assets/fonts/MaterialIcons-Regular.otf",
                dist / "assets/fonts/MaterialIcons-Regular.otf")
    shutil.copy(web / "python-worker.js", dist / "python-worker.js")
    with tarfile.open(dist / "app.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("main.py")
        member.size = 0
        archive.addfile(member, io.BytesIO(b""))

    done = subprocess.run(
        [sys.executable, str(root / "tools/publish_ipfs.py"),
         "--dry-run", "--no-build", "--dist", str(dist)],
        capture_output=True, text=True, cwd=root, timeout=120,
        # The return code is asserted below, with the output to explain it.
        check=False,
    )

    assert "ModuleNotFoundError" not in done.stderr, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    # And it did the work rather than merely starting: the font is cut.
    cut = (dist / "assets/fonts/MaterialIcons-Regular.otf").stat().st_size
    assert cut < 50_000, f"the font was not subset ({cut} bytes)"


def test_publishing_compiles_the_marks_first() -> None:
    """The assets are generated and gitignored, which is right -- and
    means a stale `src/assets/curve` shows up in no `git status` and no
    diff. Nobody should have to remember it.

    Order matters too: `flet publish` tars `src/` into the app package,
    so marks compiled after it would not be in the build.
    """
    source = Path(ipfs.__file__).read_text(encoding="utf-8")

    assert "build_assets.py" in source, "publishing must compile the marks"
    body = source[source.index("if not options.no_build:") :]
    assert body.index("compile_assets()") < body.index("publish()"), (
        "the marks have to be compiled before `flet publish` packs src/"
    )


# -- what a gateway will not serve ------------------------------------------
#
# Two rules, measured separately, and neither predicts the other. gzip is
# caught by its *bytes* -- a text file named `.tar.gz` is served and a real
# archive under that name is not. `.zip` is caught by its *name*: a wheel is
# `PK\x03\x04` exactly as `python_stdlib.zip` is, and eth.limo serves the
# wheel and refuses the zip from the same directory in the same pin.


def test_the_stdlib_zip_stops_the_upload(tmp_path: Path) -> None:
    """The one that matters, and it is not ours to rename.

    Pyodide fetches it during `loadPyodide()` as
    `indexURL + "python_stdlib.zip"`, so a gateway refusing it means the
    shell loads and the Python never arrives -- one 404 among a hundred
    200s, which reads as a slow site rather than a broken one.
    """
    root = build(tmp_path, {"index.html": "<html>", "pyodide/python_stdlib.zip": "PK"})
    assert ipfs.refused_by_gateway(root) == ["pyodide/python_stdlib.zip"]


def test_a_wheel_is_the_same_bytes_and_is_left_alone(tmp_path: Path) -> None:
    """Which is why the rule is the suffix and not the magic number."""
    root = build(tmp_path, {"pyodide/packaging-26.1-py3-none-any.whl": "PK\x03\x04"})
    assert ipfs.refused_by_gateway(root) == []


def test_a_build_with_nothing_refused_is_quiet(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "main.dart.js": "//"})
    assert ipfs.refused_by_gateway(root) == []


# -- proving the pin before a name points at it -----------------------------


def test_the_lazy_token_art_is_not_verified(tmp_path: Path) -> None:
    """6,716 files a visitor does not need to boot. Asking a public
    gateway for all of them is an imposition, not a check."""
    root = build(
        tmp_path,
        {
            "index.html": "<html>",
            "pyodide/pyodide.js": "//",
            "curve/tokens/ethereum/0xabc@20.png": "x",
        },
    )
    assert ipfs.boot_files(root) == ["index.html", "pyodide/pyodide.js"]


def test_a_fast_failure_is_a_refusal_and_a_slow_one_is_not_yet() -> None:
    """The whole point of the gate: only one of these clears by waiting.

    Measured on the pin that prompted this -- a refused file answers 404
    in ~0.3s, a block whose providers are not announced yet spends the
    gateway's whole retrieval budget first and comes back 504 at ~17s.
    """
    assert ipfs.classify(404, 0.3) == "refused"
    assert ipfs.classify(504, 17.5) == "unfound"
    assert ipfs.classify(200, 0.4) == "served"
    assert ipfs.classify(206, 1.2) == "served"
    # A timeout has no status code at all, and is still not a refusal.
    assert ipfs.classify("ReadTimeout", 45.0) == "unfound"


class FakeGateway:
    """Answers each path from a script, one entry per attempt."""

    def __init__(self, answers: dict[str, list[tuple[int, float]]]) -> None:
        self.answers = {k: list(v) for k, v in answers.items()}
        self.asked: list[str] = []

    def probe(self, _client, url: str) -> tuple[int, float]:
        path = url.split(".ipfs.dweb.link/", 1)[-1]
        self.asked.append(path)
        queue = self.answers.get(path, [(200, 0.1)])
        return queue.pop(0) if len(queue) > 1 else queue[0]


def run_verify(monkeypatch, gateway: FakeGateway, paths: list[str], **kw) -> dict:
    monkeypatch.setattr(ipfs, "probe", gateway.probe)
    return ipfs.verify(
        "bafyfake", paths, client=object(), sleep=lambda _s: None, **kw
    )


def test_a_pin_that_is_fully_retrievable_passes(monkeypatch) -> None:
    gateway = FakeGateway({"index.html": [(200, 0.2)], "main.dart.js": [(206, 0.4)]})
    assert run_verify(monkeypatch, gateway, ["index.html", "main.dart.js"]) == {}


def test_a_file_still_propagating_is_retried_until_it_lands(monkeypatch) -> None:
    """This is the case the deadline exists for, and it must not fail."""
    gateway = FakeGateway({"slow.js": [(504, 17.0), (504, 17.0), (200, 0.9)]})

    assert run_verify(monkeypatch, gateway, ["slow.js"]) == {}
    assert gateway.asked == ["slow.js", "slow.js", "slow.js"]


def test_a_refusal_is_never_retried(monkeypatch) -> None:
    """A gateway declining a suffix will decline it for the rest of the day,
    so retrying is a quarter of an hour spent learning nothing."""
    gateway = FakeGateway({"pyodide/python_stdlib.zip": [(404, 0.3)]})

    bad = run_verify(monkeypatch, gateway, ["pyodide/python_stdlib.zip"])

    assert bad["pyodide/python_stdlib.zip"][0] == "refused"
    assert gateway.asked == ["pyodide/python_stdlib.zip"], "asked more than once"


def test_the_deadline_ends_a_pin_that_never_propagates(monkeypatch) -> None:
    gateway = FakeGateway({"never.js": [(504, 17.0)]})
    ticks = iter([0.0, 0.0, 10.0, 999.0, 999.0])

    bad = run_verify(
        monkeypatch, gateway, ["never.js"], deadline=60.0, now=lambda: next(ticks)
    )

    assert bad["never.js"][0] == "unfound"


def test_a_file_that_heals_is_dropped_from_the_report(monkeypatch) -> None:
    """It failed on the first pass; the report is about the end state."""
    gateway = FakeGateway({"a.js": [(504, 17.0), (200, 0.3)], "b.js": [(200, 0.1)]})
    assert run_verify(monkeypatch, gateway, ["a.js", "b.js"]) == {}


# -- the CID comes first, and the waiting is its own phase ------------------
#
# The upload succeeding and the network being able to find the result are
# two questions with two answers, and the second can legitimately take a
# quarter of an hour. Holding the CID back behind it makes a slow network
# read as a failed publish.


def test_the_bar_fills_with_what_is_retrievable() -> None:
    assert "[" + "-" * 28 + "]" in ipfs.progress_bar(0, 92, 0.0)
    assert "[" + "#" * 28 + "]" in ipfs.progress_bar(92, 92, 1.0)
    assert "46/92 retrievable" in ipfs.progress_bar(46, 92, 0.0)
    assert "4m03s" in ipfs.progress_bar(1, 92, 243.0)


def test_an_empty_build_does_not_divide_by_zero() -> None:
    assert ipfs.progress_bar(0, 0, 0.0)


def test_the_bar_redraws_in_place_only_on_a_terminal() -> None:
    """A `\\r` bar piped to a file is thousands of overwritten lines."""

    class Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    piped, terminal = Pipe(), Terminal()
    for stream in (piped, terminal):
        report = ipfs.progress_reporter(stream)
        report(1, 2, 0.0)
        report(2, 2, 1.0)

    assert "\r" not in piped.getvalue()
    assert piped.getvalue().count("\n") == 2
    assert terminal.getvalue().count("\r") == 2
    assert "\n" not in terminal.getvalue()


def test_the_cid_is_printed_before_any_waiting(capsys) -> None:
    """The one thing worth having from the run, whatever happens next."""
    ipfs.show_pin("bafyexample", duplicate=True)
    out = capsys.readouterr().out

    assert "bafyexample" in out
    assert "already pinned" in out
    assert "https://bafyexample.ipfs.dweb.link/" in out
    assert "ipfs://bafyexample/" in out


def waiting_options(**kw):
    import types

    return types.SimpleNamespace(
        verify_gateway=ipfs.VERIFY_GATEWAY, verify_deadline=90.0, **kw
    )


def test_interrupting_the_wait_keeps_the_pin_and_says_how_to_resume(
    monkeypatch, capsys
) -> None:
    """Ctrl-C during a long wait must not read as a failed publish."""

    def interrupted(*_a, **_kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(ipfs, "verify", interrupted)

    code = ipfs.wait_until_findable("bafyexample", ["index.html"], waiting_options())
    out = capsys.readouterr().out

    assert code == 130
    assert "The pin is fine" in out
    assert "--verify-only bafyexample" in out


def test_a_pin_still_propagating_says_to_come_back(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        ipfs, "verify", lambda *a, **kw: {"slow.js": ("unfound", 504, 17.0)}
    )

    code = ipfs.wait_until_findable("bafyexample", ["slow.js"], waiting_options())
    out = capsys.readouterr().out

    assert code == 1
    assert "not found yet" in out
    assert "Do not point ENS at this CID yet" in out
    assert "--verify-only bafyexample" in out


def test_a_refusal_does_not_suggest_waiting_longer(monkeypatch, capsys) -> None:
    """Resuming would burn another deadline on a decision already made."""
    monkeypatch.setattr(
        ipfs, "verify", lambda *a, **kw: {"a.zip": ("refused", 404, 0.3)}
    )

    code = ipfs.wait_until_findable("bafyexample", ["a.zip"], waiting_options())
    out = capsys.readouterr().out

    assert code == 1
    assert "waiting will not help" in out
    assert "--verify-only" not in out


def test_a_pin_the_network_can_find_reports_the_wait_and_passes(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(ipfs, "verify", lambda *a, **kw: {})

    code = ipfs.wait_until_findable("bafyexample", ["a", "b"], waiting_options())
    out = capsys.readouterr().out

    assert code == 0
    assert "all 2 retrievable" in out
    assert "safe to point ENS at this CID" in out
