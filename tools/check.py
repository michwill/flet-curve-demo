#!/usr/bin/env python3
"""Everything that has an opinion about this code, in one command.

    python tools/check.py            # ruff, mypy, pytest
    python tools/check.py --fix      # let ruff fix what it safely can first

Three tools, in the order that fails fastest:

  * **ruff** for the things a linter sees -- it is the one that found an
    `except WalletError` in a module that never imported the name, which
    would have raised `NameError` from the handler meant to swallow an
    error;
  * **mypy** for the things only a type checker sees. The app code and the
    tools are checked without exception; the tests are checked for
    everything except the codes a test double trips by existing;
  * **pytest** last, because it is the slowest and the other two catch
    whole classes of failure without running anything.

Configuration for all three lives in `pyproject.toml`, so an editor
integration and this script agree by construction.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("src", "tools", "tests")


def run(label: str, command: list[str]) -> bool:
    print(f"\n\033[1m{label}\033[0m  ({' '.join(command[2:])})", flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true", help="apply ruff's safe fixes before checking"
    )
    options = parser.parse_args()

    python = [sys.executable, "-m"]
    fix = ["--fix"] if options.fix else []
    steps = [
        ("ruff", [*python, "ruff", "check", *TARGETS, *fix]),
        ("mypy", [*python, "mypy"]),
        ("pytest", [*python, "pytest", "-q"]),
    ]
    failed = [label for label, command in steps if not run(label, command)]
    print()
    if failed:
        print(f"\033[31mfailed: {', '.join(failed)}\033[0m")
        return 1
    print("\033[32mall checks passed\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
