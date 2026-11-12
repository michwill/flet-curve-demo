"""`tools/build_router.py`: the wasm-bindgen pin, and steps that fail alone."""

import sys

import pytest

from tools import build_router


def test_pin_matches_the_submodule_manifest():
    pin = build_router.wasm_bindgen_pin()
    manifest = (build_router.SOURCE / "rust/wasm/Cargo.toml").read_text()
    assert f'wasm-bindgen = "={pin}"' in manifest


@pytest.mark.parametrize("spec", ['"=0.2.129"', '{ version = "=0.2.129" }'])
def test_pin_reads_either_dependency_form(tmp_path, monkeypatch, spec):
    (tmp_path / "rust/wasm").mkdir(parents=True)
    (tmp_path / "rust/wasm/Cargo.toml").write_text(
        f"[dependencies]\nwasm-bindgen = {spec}\n")
    monkeypatch.setattr(build_router, "SOURCE", tmp_path)
    assert build_router.wasm_bindgen_pin() == "0.2.129"


def test_matching_cli_installs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(build_router, "wasm_bindgen_pin", lambda: "0.2.129")
    monkeypatch.setattr(build_router, "wasm_bindgen_version", lambda path: "0.2.129")
    monkeypatch.setattr(build_router.subprocess, "run",
                        lambda *a, **k: pytest.fail("ran a subprocess"))
    build_router.ensure_wasm_bindgen({"PATH": str(tmp_path)})


def test_mismatched_cli_is_cargo_installed_at_the_pin(monkeypatch):
    versions = iter(["0.2.127", "0.2.129"])
    calls = []
    monkeypatch.setattr(build_router, "wasm_bindgen_pin", lambda: "0.2.129")
    monkeypatch.setattr(build_router, "wasm_bindgen_version",
                        lambda path: next(versions))
    def run(cmd, **kwargs):
        calls.append(cmd)
        return build_router.subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(build_router.subprocess, "run", run)
    build_router.ensure_wasm_bindgen({"PATH": ""})
    assert calls == [["cargo", "install", "wasm-bindgen-cli",
                      "--version", "=0.2.129", "--locked"]]


def test_shadowed_install_is_an_error(monkeypatch):
    monkeypatch.setattr(build_router, "wasm_bindgen_pin", lambda: "0.2.129")
    monkeypatch.setattr(build_router, "wasm_bindgen_version", lambda path: "0.2.127")
    monkeypatch.setattr(build_router.subprocess, "run",
                        lambda cmd, **k: build_router.subprocess.CompletedProcess(cmd, 0))
    with pytest.raises(build_router.BuildError, match=r"still finds 0\.2\.127"):
        build_router.ensure_wasm_bindgen({"PATH": ""})


def test_native_builds_when_the_wasm_fails(monkeypatch):
    built = []
    monkeypatch.setattr(sys, "argv", ["build_router.py", "--native"])
    monkeypatch.setattr(build_router, "check_submodule", lambda: None)
    monkeypatch.setattr(build_router, "copy_package", lambda: 0)
    monkeypatch.setattr(build_router, "copy_data", lambda: (0, 0))

    def wasm(rebuild):
        raise build_router.BuildError("wasm broke")

    monkeypatch.setattr(build_router, "build_wasm", wasm)
    monkeypatch.setattr(build_router, "build_native", lambda: built.append(True))
    with pytest.raises(SystemExit, match="wasm broke"):
        build_router.main()
    assert built == [True]


def test_both_failures_are_reported(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build_router.py", "--native"])
    monkeypatch.setattr(build_router, "check_submodule", lambda: None)
    monkeypatch.setattr(build_router, "copy_package", lambda: 0)
    monkeypatch.setattr(build_router, "copy_data", lambda: (0, 0))

    def fail(message):
        def step(*args):
            raise build_router.BuildError(message)
        return step

    monkeypatch.setattr(build_router, "build_wasm", fail("wasm broke"))
    monkeypatch.setattr(build_router, "build_native", fail("maturin broke"))
    with pytest.raises(SystemExit) as exc:
        build_router.main()
    assert "wasm broke" in str(exc.value) and "maturin broke" in str(exc.value)
