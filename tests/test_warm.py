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
        "boot": False,
        "all_marks": False,
        "dist": Path("."),
        "chunk": warm.CHUNK,
        "workers": warm.MARK_WORKERS,
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


def test_the_marks_are_the_default_and_the_boot_set_is_not(tmp_path: Path) -> None:
    """Bytes, not taste. On this build the boot set is 60.7 MB against the
    marks' 10.9 -- 85% of the weight, warmed on every publish already, and
    at an observed 2 KB/s it is eight hours on its own. The marks are the
    half nothing else ever warms."""
    root = build(tmp_path, {"xdai": ["0xaa@80.png"]})

    assert warm.plan(root, options(all_marks=True)) == ["curve/tokens/xdai/0xaa@80.png"]


def test_the_bundles_are_warmed_and_the_loose_marks_are_not(tmp_path: Path) -> None:
    """A browser fetches one pair per chain now, not up to 627 files, so
    that pair is what warming is for. The loose marks stay reachable as
    the fallback -- 3,358 of them against 136 bundles -- and are worth
    warming eventually rather than first."""
    root = build(tmp_path, {"xdai": ["0xaa@80.png", "marks@80.bin", "marks@80.json"]})

    assert warm.plan(root, options()) == [
        "curve/tokens/xdai/marks@80.bin",
        "curve/tokens/xdai/marks@80.json",
    ]
    assert "curve/tokens/xdai/0xaa@80.png" in warm.plan(root, options(all_marks=True))


def test_the_bundles_come_before_the_marks_they_back(tmp_path: Path) -> None:
    root = build(tmp_path, {"xdai": ["0xaa@80.png", "marks@80.bin", "marks@80.json"]})

    paths = warm.plan(root, options(all_marks=True))

    assert paths.index("curve/tokens/xdai/marks@80.bin") < paths.index(
        "curve/tokens/xdai/0xaa@80.png"
    )


def test_the_boot_set_can_be_added_and_comes_first(tmp_path: Path) -> None:
    """It decides whether the site loads, where the marks only decide
    whether it looks right, so an interrupted run should buy it first."""
    root = build(tmp_path, {"xdai": ["0xaa@80.png"]})

    paths = warm.plan(root, options(boot=True, all_marks=True))

    assert paths[:2] == ["index.html", "main.dart.js"]
    assert paths[-1] == "curve/tokens/xdai/0xaa@80.png"


def test_either_half_can_be_asked_for_alone(tmp_path: Path) -> None:
    root = build(tmp_path, {"xdai": ["0xaa@80.png"]})

    assert warm.plan(root, options(boot=True, boot_only=True)) == [
        "index.html", "main.dart.js"
    ]
    assert warm.plan(root, options(all_marks=True)) == ["curve/tokens/xdai/0xaa@80.png"]


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
    assert seen["workers"] == warm.MARK_WORKERS
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


def test_the_size_is_stated_up_front(monkeypatch, tmp_path, capsys) -> None:
    """Files alone hide the shape of this: 77 boot files outweigh 3,358
    marks six to one, so a run reported purely in files sits at 0/3435
    through the slowest part of its work."""
    root = build(tmp_path)
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot-only"])
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **_k: {})

    warm.main()

    assert "MB" in capsys.readouterr().out


def test_the_rate_is_measured_rather_than_predicted() -> None:
    """Two readings of the same gateway hours apart came in at 686 KB/s and
    2 KB/s. A prediction from either would be a confident lie about the
    other, so nothing is predicted -- it reports what it is achieving."""
    assert warm.rate_text(0, 100, 0, 5.0) == ""
    assert warm.rate_text(100, 100, 1000, 0.0) == ""

    assert "left" in warm.rate_text(50, 100, 1024 * 50, 60.0)


def test_files_a_second_leads_because_the_byte_rate_is_meaningless() -> None:
    """A mark is 3.2 KB and its transfer takes 0.0001s against a 0.6s
    time-to-first-byte, so the cost is all lookup: you would need 308
    files a second to show 1 MB/s. Reporting KB/s alone produced a healthy
    run advertising "8 KB/s", which reads as a broken connection."""
    text = warm.rate_text(128, 3358, 8 * 1024 * 54, 54.0)

    assert text.index("files/s") < text.index("KB/s")
    assert "2.4 files/s" in text


def test_the_marks_get_more_workers_than_the_boot_set(monkeypatch, tmp_path) -> None:
    """Measured rather than inherited: three disjoint slices of forty cold
    marks ran at 0.59, 0.71 and 1.11 files/s on 2, 4 and 8 workers, and
    nothing was throttled at any of them. The two-worker limit was earned
    pulling multi-megabyte boot files, which is a different load."""
    root = build(tmp_path, {"xdai": ["marks@80.bin", "marks@80.json"]})
    seen = {}
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **kw: seen.update(kw) or {})

    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root)])
    warm.main()
    assert seen["workers"] == warm.MARK_WORKERS == 8

    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--boot"])
    warm.main()
    assert seen["workers"] == ipfs.WARM_WORKERS == 2


def test_the_worker_count_can_be_overridden(monkeypatch, tmp_path) -> None:
    root = build(tmp_path, {"xdai": ["marks@80.bin", "marks@80.json"]})
    seen = {}
    monkeypatch.setattr(warm, "verify", lambda _c, _p, **kw: seen.update(kw) or {})
    monkeypatch.setattr("sys.argv", ["warm_ipfs.py", "--dist", str(root), "--workers", "3"])

    warm.main()

    assert seen["workers"] == 3


def test_the_weight_is_what_the_run_will_actually_pull(tmp_path: Path) -> None:
    root = build(tmp_path)

    assert warm.weight(root, ["index.html", "main.dart.js"]) > 0
    assert warm.weight(root, ["not-in-this-build.png"]) == 0


def test_no_time_left_is_no_estimate() -> None:
    """A finished run does not advertise how long it has to go."""
    assert warm.remaining_text(0, 100, 10.0) == ""
    assert warm.remaining_text(100, 100, 10.0) == ""
    assert "left" in warm.remaining_text(50, 100, 60.0)


def test_both_halves_of_a_split_chain_are_warmed(tmp_path: Path) -> None:
    """Ethereum ships its marks in two: 658 KB that gates the first paint
    and 2,194 KB that fills in behind it. Warming only the first would
    leave every mark past the hottest 150 cold."""
    root = build(
        tmp_path,
        {"ethereum": ["marks@80.bin", "marks@80.json",
                      "marks@80-rest.bin", "marks@80-rest.json"]},
    )

    paths = warm.bundle_files(root, (80,), [])

    assert paths == [
        "curve/tokens/ethereum/marks@80.bin",
        "curve/tokens/ethereum/marks@80.json",
        "curve/tokens/ethereum/marks@80-rest.bin",
        "curve/tokens/ethereum/marks@80-rest.json",
    ]


def test_a_chain_with_no_tail_is_not_asked_for_one(tmp_path: Path) -> None:
    root = build(tmp_path, {"xdai": ["marks@80.bin", "marks@80.json"]})

    assert warm.bundle_files(root, (80,), []) == [
        "curve/tokens/xdai/marks@80.bin",
        "curve/tokens/xdai/marks@80.json",
    ]


def with_chain_marks(root: Path, names: list[str]) -> Path:
    """The network marks as `build_assets` writes them: one file per
    network per tier, beside the bundle they are the fallback for."""
    directory = root / "curve" / "chains"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        for tier in (20, 40, 80, 160):
            (directory / f"{name}@{tier}.png").write_bytes(b"\x89PNG")
    for tier in (40, 80):
        (directory / f"marks@{tier}.bin").write_bytes(b"\x89PNG")
        (directory / f"marks@{tier}.json").write_text("{}")
    return root


def test_the_network_marks_are_warmed_at_every_tier(tmp_path: Path) -> None:
    """Not just the two `DEFAULT_TIERS` picks, because the fallback here
    is on the path of every visit: the picker's field is built before any
    bundle exists and asks for the *top* tier, and its menu asks for
    whichever tier the screen's ratio lands on."""
    root = with_chain_marks(build(tmp_path), ["ethereum", "xdai"])

    assert warm.chain_files(root) == [
        "curve/chains/ethereum@160.png",
        "curve/chains/ethereum@20.png",
        "curve/chains/ethereum@40.png",
        "curve/chains/ethereum@80.png",
        "curve/chains/xdai@160.png",
        "curve/chains/xdai@20.png",
        "curve/chains/xdai@40.png",
        "curve/chains/xdai@80.png",
    ]


def test_the_network_marks_are_warmed_without_being_asked_for(tmp_path: Path) -> None:
    """Unlike the 3,358 token marks behind `--all-marks`. 160 files for
    the one family that appears on every screen, and the family a blank
    circle in the network menu was traced to."""
    root = with_chain_marks(build(tmp_path), ["ethereum"])

    paths = warm.plan(root, options())

    assert "curve/chains/ethereum@160.png" in paths
    assert "curve/chains/marks@80.bin" in paths
    # Bundles still lead: they are what a browser actually fetches.
    assert paths.index("curve/chains/marks@80.bin") < paths.index(
        "curve/chains/ethereum@160.png"
    )


def test_naming_chains_still_means_those_chains_marks(tmp_path: Path) -> None:
    """`--chains xdai` is a way to warm one network's coins, and pulling
    every network's own logo in on top of that would ignore it."""
    root = with_chain_marks(build(tmp_path, {"xdai": ["marks@80.bin"]}), ["ethereum"])

    paths = warm.plan(root, options(chains=["xdai"]))

    assert paths == ["curve/tokens/xdai/marks@80.bin"]
