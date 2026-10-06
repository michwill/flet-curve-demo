"""Fixtures every test gets, wanted or not."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _private_state(tmp_path_factory):
    """`XDG_STATE_HOME` for the whole run, so nothing a test provokes lands in ~/.local/state."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
        yield
