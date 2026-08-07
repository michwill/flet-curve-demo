"""Whether the app may connect a wallet without being asked.

Disconnecting is a decision, and it has to outlive the process that heard
it. Without that, the desktop build reconnects on the next launch -- it
connects at startup by design, because a local wallet raises no popup --
and the user who just disconnected is connected again with no way to say
otherwise short of quitting the wallet.

So a deliberate disconnect leaves a marker, and connecting removes it. The
marker is a file rather than a setting because the wallet package has no
UI and no storage of its own, and because the state is per machine, not
per app window.

The browser does not use this: there, "do not reconnect" is the absence of
a remembered wallet in the page's own storage (see `wallet_bridge.js`), and
`autoconnect` is never true anyway -- asking for accounts unprompted on
page load is hostile.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

#: Name of the marker. Its presence means "the user disconnected".
_MARKER = "disconnected"


def state_dir() -> Path:
    """Where per-machine state lives, following the XDG convention."""
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "flet-curve"


def _marker() -> Path:
    return state_dir() / _MARKER


def autoconnect_allowed() -> bool:
    """False once the user has disconnected, until they connect again."""
    try:
        return not _marker().exists()
    except OSError:
        # An unreadable state directory is not a reason to refuse to work.
        return True


def record_disconnect() -> None:
    """Remember that the user disconnected deliberately."""
    # Read-only home, or no home at all. The session still ends; only the
    # memory of it is lost.
    with contextlib.suppress(OSError):
        path = _marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def record_connect() -> None:
    """Forget it: connecting is the answer to the question it asked."""
    with contextlib.suppress(OSError):
        _marker().unlink()
