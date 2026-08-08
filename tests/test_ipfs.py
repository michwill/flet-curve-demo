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

import io
import json
import tarfile
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


def test_the_app_package_is_renamed_and_the_page_follows(tmp_path: Path) -> None:
    """eth.limo answers 404 for `app.tar.gz` and 200 for everything else in
    the same pin, so the app loads its shell, never gets its Python, and
    sits on "Working..." forever. The worker takes `.tgz` just as happily."""
    root = build(
        tmp_path,
        {
            "index.html": '<head><base href="./"></head>'
            '<script>appPackageUrl: "app.tar.gz"</script>',
            "app.tar.gz": "not really gzip, but a file",
        },
    )

    assert ipfs.rename_package(root) is True
    assert (root / "app.tgz").is_file()
    assert not (root / "app.tar.gz").exists()
    assert '"app.tgz"' in (root / "index.html").read_text(encoding="utf-8")


def test_renaming_a_build_that_is_already_renamed_does_nothing(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "x", "app.tgz": "already done"})
    assert ipfs.rename_package(root) is False


def test_the_file_is_not_renamed_without_the_line_that_points_at_it(
    tmp_path: Path,
) -> None:
    """Renaming one and not the other is a build that fetches a file that
    is not there -- the same blank page, with no 404 to explain it."""
    root = build(tmp_path, {"index.html": "<head>nothing here</head>", "app.tar.gz": "x"})
    with pytest.raises(SystemExit, match="appPackageUrl"):
        ipfs.rename_package(root)
    assert (root / "app.tar.gz").is_file()


def test_probes_cover_the_suffixes_worth_asking_about(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "x"})
    names = ipfs.add_probes(root)

    assert f"{ipfs.PROBE_STEM}.tar.gz" in names  # the one that is refused
    assert f"{ipfs.PROBE_STEM}.txt" in names  # the control
    assert all((root / name).is_file() for name in names)


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


def test_a_clean_build_reports_nothing(tmp_path: Path) -> None:
    root = build(tmp_path, {"index.html": "<html>", "main.dart.js": "console.log(1)"})
    assert ipfs.leaked(root, "SEKRIT") == []


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
