"""The warmer that runs between publishes.

Warming decays -- an edge's store is a cache and there are several edges
behind one name -- so `publish_ipfs --warm` fixing things on the day it
runs says nothing about the week after. Measured: `main.dart.wasm` is in
the boot set and was warmed on the 15th, and on the 18th eth.limo answered
504 for it after seventeen seconds. This script exists to be run again.

Two things it does that publishing deliberately does not: both gateways,
and the token marks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import publish_ipfs as ipfs
from tools import warm_ipfs as warm


def build(root: Path, marks: dict[str, list[str]] | None = None) -> Path:
    """A `dist/` with a boot set and, optionally, some compiled marks."""
    (root / "index.html").write_text("<!doctype html>")
    (root / "main.dart.js").write_text("//")
    for chain, files in (marks or {}).items():
        directory = root / "curve" / "tokens" / chain
        directory.mkdir(parents=True, exist_ok=True)
        for name in files:
            (directory / name).write_bytes(b"\x89PNG")
    return root


def options(**kw):
    """The parsed arguments, with the defaults the CLI would supply."""
    base = {
        "tiers": warm.DEFAULT_TIERS,
        "chains": [],
        "boot_only": False,
        "marks_only": False,
        "chunk": warm.CHUNK,
        "deadline": warm.WARM_DEADLINE,
        "show": 20,
    }
    base.update(kw)
    return type("Options", (), base)()


# -- what gets asked for ---------------------------------------------------


def test_the_marks_are_what_publishing_leaves_out(tmp_path: Path) -> None:
    """`LAZY_DIR` skips 6,716 files on every publish, and is right to --
    but that reasoning is about publishing. Nothing had ever warmed them,
    which is why a missing coin logo is the most visible form of this."""
    root = build(tmp_path, {"xdai": ["0xaa@40.png", "0xaa@80.png"]})

    assert ipfs.boot_files(root) == ["index.html", "main.dart.js"]
    assert warm.mark_files(root, (40, 80), []) == [
        "curve/tokens/xdai/0xaa@40.png",
        "curve/tokens/xdai/0xaa@80.png",
    ]


def test_only_the_tiers_asked_for(tmp_path: Path) -> None:
    """A mark is compiled at four sizes and a screen uses one of them.
    Warming all four doubles the work to cover the ends of the range."""
    root = build(tmp_path, {"xdai": [f"0xaa@{t}.png" for t in (20, 40, 80, 160)]})

    assert warm.mark_files(root, (80,), []) == ["curve/tokens/xdai/0xaa@80.png"]
    assert len(warm.mark_files(root, (20, 40, 80, 160), [])) == 4


def test_a_chain_filter_narrows_it(tmp_path: Path) -> None:
    root = build(tmp_path, {"xdai": ["0xaa@80.png"], "ethereum": ["0xbb@80.png"]})

    assert warm.mark_files(root, (80,), ["xdai"]) == ["curve/tokens/xdai/0xaa@80.png"]
    assert len(warm.mark_files(root, (80,), [])) == 2


def test_the_order_is_stable_across_runs(tmp_path: Path) -> None:
    """An interrupted run resumes over roughly the same ground rather than
    sampling a fresh scatter of a 6,716-file set."""
    root = build(tmp_path, {"b": ["0x02@80.png", "0x01@80.png"], "a": ["0x03@80.png"]})

    assert warm.mark_files(root, (80,), []) == warm.mark_files(root, (80,), [])
    assert warm.mark_files(root, (80,), []) == [
        "curve/tokens/a/0x03@80.png",
        "curve/tokens/b/0x01@80.png",
        "curve/tokens/b/0x02@80.png",
    ]


def test_a_build_with_no_marks_compiled_is_not_an_error(tmp_path: Path) -> None:
    """`build_assets.py` is a separate step and skipping it is allowed."""
    assert warm.mark_files(build(tmp_path), (80,), []) == []


def test_the_boot_set_is_asked_for_first(tmp_path: Path) -> None:
    """Ctrl-C after ten minutes should have bought the files that decide
    whether the site loads, not a scatter of coin logos."""
    root = build(tmp_path, {"xdai": ["0xaa@80.png"]})

    paths = warm.plan(root, options())

    assert paths[:2] == ["index.html", "main.dart.js"]
    assert paths[-1] == "curve/tokens/xdai/0xaa@80.png"


def test_either_half_can_be_asked_for_alone(tmp_path: Path) -> None:
    root = build(tmp_path, {"xdai": ["0xaa@80.png"]})

    assert warm.plan(root, options(boot_only=True)) == ["index.html", "main.dart.js"]
    assert warm.plan(root, options(marks_only=True)) == ["curve/tokens/xdai/0xaa@80.png"]


# -- tiers -----------------------------------------------------------------


def test_tiers_are_read_the_way_a_person_writes_them() -> None:
    assert warm.parse_tiers("80") == (80,)
    assert warm.parse_tiers("40,80") == (40, 80)
    assert warm.parse_tiers("40, 80") == (40, 80)


def test_all_means_whatever_the_app_compiles() -> None:
    """Not a second copy of the list. `MARK_TIERS` is the app's own, and a
    tier added there would otherwise be one this never warmed."""
    from ui.assets import MARK_TIERS

    assert warm.parse_tiers("all") == MARK_TIERS
    assert set(warm.DEFAULT_TIERS) <= set(MARK_TIERS)


# -- the gateways ----------------------------------------------------------


def test_both_gateways_are_warmed_by_default() -> None:
    """They are separate infrastructure behind one name, with separate
    caches, and a visitor does not choose between them. Warming one leaves
    half the audience exactly where they started."""
    assert "https://curve.eth.limo" in warm.GATEWAYS
    assert "https://curve.eth.link" in warm.GATEWAYS


def test_it_pulls_whole_files_rather_than_sampling(monkeypatch) -> None:
    """The difference between measuring and warming: a block only stays in
    an edge's store if it was actually fetched."""
    seen: dict = {}

    def fake_verify(_cid, paths, **kw):
        seen.update(kw)
        seen["paths"] = paths
        return {}

    monkeypatch.setattr(warm, "verify", fake_verify)
    warm.warm_one("https://curve.eth.limo", ["index.html"], options(deadline=1, show=5))

    assert seen["whole"] is True
    assert seen["workers"] == ipfs.WARM_WORKERS == 2
    assert seen["gateway"] == "https://curve.eth.limo"


def test_a_gateway_that_will_not_serve_something_is_reported(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        warm, "verify", lambda _c, _p, **_k: {"main.dart.wasm": ("unfound", 504, 17.6)}
    )
    bad = warm.warm_one("https://curve.eth.limo", ["main.dart.wasm"], options(deadline=1, show=5))

    out = capsys.readouterr().out
    assert bad
    assert "main.dart.wasm" in out and "504" in out


def test_a_long_failure_list_is_truncated(monkeypatch, capsys) -> None:
    """A thousand cold marks must not bury the summary line."""
    failures = {f"curve/tokens/xdai/{i}@80.png": ("unfound", 504, 17.0) for i in range(50)}
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: failures)

    warm.warm_one("https://x", list(failures), options(deadline=1, show=5))

    out = capsys.readouterr().out
    assert "... and 45 more" in out


# -- the script itself -----------------------------------------------------


def test_asking_for_nothing_is_refused(monkeypatch, tmp_path: Path) -> None:
    """`--boot-only --marks-only` is not an empty warm, it is a mistake."""
    monkeypatch.setattr(
        "sys.argv", ["warm_ipfs.py", "--dist", str(build(tmp_path)), "--boot-only", "--marks-only"]
    )
    with pytest.raises(SystemExit) as caught:
        warm.main()
    assert caught.value.code == 2


def test_no_build_says_so_rather_than_warming_nothing(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(tmp_path / "gone")])

    assert warm.main() == 2
    assert "no build" in capsys.readouterr().out


def test_everything_served_exits_zero(monkeypatch, tmp_path: Path) -> None:
    root = build(tmp_path)
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot-only"])
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {})

    assert warm.main() == 0


def test_anything_left_cold_exits_nonzero(monkeypatch, tmp_path: Path, capsys) -> None:
    """So a scheduled run can be noticed when it stops being enough."""
    root = build(tmp_path)
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot-only"])
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {"index.html": ("unfound", 504, 17.0)})

    assert warm.main() == 1
    assert "drifted" in capsys.readouterr().out


def test_an_interrupt_is_a_supported_way_to_leave(monkeypatch, tmp_path: Path, capsys) -> None:
    """Every file fetched before the interrupt stays fetched, so stopping
    early is a partial success and must not read as a crash."""
    root = build(tmp_path)
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot-only"])

    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(warm, "verify", interrupt)

    assert warm.main() == 130
    assert "stays warm" in capsys.readouterr().out


# -- reporting while it works ----------------------------------------------


def test_progress_is_reported_while_it_works_not_at_the_end(monkeypatch, capsys) -> None:
    """The bug this cadence exists for. `verify` reports once per pass,
    which is right for the 77-file boot set at forty-five seconds a pass
    and wrong for 3,435 files at thirty-four minutes: the first version
    printed its header and then nothing for half an hour, which is
    indistinguishable from a hang and was reported as one.
    """
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {})
    paths = [f"f{i}.png" for i in range(200)]

    warm.warm_one("https://x", paths, options(chunk=64))

    bars = [line for line in capsys.readouterr().out.splitlines() if "retrievable" in line]
    assert len(bars) >= 4  # 200 files in batches of 64, not one line at the end


def test_the_batches_cover_every_file_exactly_once() -> None:
    assert warm.batched([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert warm.batched([], 64) == []
    assert sum(len(b) for b in warm.batched(list(range(3435)), 64)) == 3435


def test_every_batch_is_asked_for(monkeypatch) -> None:
    """Batching is a reporting change, not a sampling one."""
    asked: list[str] = []

    def fake_verify(_cid, paths, **_kw):
        asked.extend(paths)
        return {}

    monkeypatch.setattr(warm, "verify", fake_verify)
    paths = [f"f{i}.png" for i in range(150)]

    warm.warm_one("https://x", paths, options(chunk=64))

    assert asked == paths


def test_one_unfindable_file_does_not_hold_up_the_rest(monkeypatch) -> None:
    """A batch retries on its own budget, not the run's. Otherwise a single
    block nobody can find keeps three thousand warm ones waiting behind
    it for the whole deadline."""
    seen: list[float] = []

    def fake_verify(_cid, _paths, **kw):
        seen.append(kw["deadline"])
        return {}

    monkeypatch.setattr(warm, "verify", fake_verify)
    warm.warm_one("https://x", ["a", "b"], options(chunk=1, deadline=7200.0))

    assert seen == [warm.CHUNK_DEADLINE, warm.CHUNK_DEADLINE]
    assert warm.CHUNK_DEADLINE < 7200.0


def test_the_run_stops_at_its_own_deadline(monkeypatch, capsys) -> None:
    """Otherwise a job meant to run between publishes runs into the next."""
    clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(warm.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {})

    warm.warm_one("https://x", ["a", "b", "c", "d"], options(chunk=2, deadline=60.0))

    assert "not reached" in capsys.readouterr().out


def test_the_estimate_is_offered_before_the_wait_not_after(monkeypatch, tmp_path, capsys) -> None:
    """Two workers is deliberate and the rate is what it is; saying so up
    front is the difference between a long job and a broken-looking one."""
    root = build(tmp_path)
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot-only"])
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {})

    warm.main()

    assert "expect roughly" in capsys.readouterr().out


def test_the_estimate_is_a_range_because_cold_is_four_times_slower() -> None:
    """Measured, both through eth.limo: the boot set warmed four days
    earlier ran at 1.7 files a second, and marks nothing had ever fetched
    at 0.46. Quoting only the fast end is how a four-hour job gets
    reported as a hang."""
    text = warm.estimate_text(3435 * 2)

    assert "67-249 minutes" in text
    fast, slow = warm.FILES_PER_SECOND
    assert fast > slow


def test_a_short_run_is_not_given_a_solemn_range() -> None:
    assert warm.estimate_text(50) == "under two minutes"


def test_no_time_left_is_no_estimate() -> None:
    """A finished run does not advertise how long it has to go."""
    assert warm.remaining_text(0, 100, 10.0) == ""
    assert warm.remaining_text(100, 100, 10.0) == ""
    assert "left" in warm.remaining_text(50, 100, 60.0)
