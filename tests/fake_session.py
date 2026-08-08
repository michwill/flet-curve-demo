"""A Flet session with no Flutter on the other end of it.

Most of the tests here build controls and read their properties back.
That finds constructor bugs and logic bugs, and it misses an entire class
of bug that only exists because Flet keeps a *previous* tree and diffs
against it -- the one that shipped: assigning to a control that a rebuild
had frozen, which raises inside an event handler where nothing can catch
it.

The diffing machinery turns out to be usable without a client. A real
update is two steps:

  * `ObjectPatch.from_diff(root, root, control_cls=BaseControl)` compares
    the tree against the snapshots taken when it was last serialised, and
    marks anything it matched **by key** as `_frozen`;
  * whatever it reports as *added* is serialised, which is what leaves the
    snapshots behind for the next diff (`protocol.py` writes
    `__prev_lists` and friends as a side effect of encoding).

Do those two, in that order, and a keyed control that was re-made behaves
exactly as it does in the browser -- `Frozen controls cannot be updated`
on the next assignment. That is what `Session.flush()` below is, and it is
what makes the stateful tests worth running.

Nothing here reaches the network, a display, or a wallet.
"""

from __future__ import annotations

from typing import Any

import flet as ft
from flet.controls.base_control import BaseControl
from flet.controls.object_patch import ObjectPatch
from flet.messaging.protocol import configure_encode_object_for_msgpack

_encode = configure_encode_object_for_msgpack(BaseControl)


def _serialise(obj: Any, seen: set[int] | None = None) -> None:
    """Encode a subtree, for the snapshots encoding leaves behind."""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, BaseControl):
        _encode(obj)
        for name in obj.__dataclass_fields__:
            _serialise(getattr(obj, name, None), seen)
    elif isinstance(obj, list):
        for item in obj:
            _serialise(item, seen)
    elif isinstance(obj, dict):
        for item in obj.values():
            _serialise(item, seen)


class Window:
    def __init__(self) -> None:
        self.width = 1280.0
        self.height = 900.0


class FakePreferences:
    """`SharedPreferences`, in a dict. Async, because the real one is."""

    def __init__(self, stored: dict[str, Any] | None = None) -> None:
        self.stored = dict(stored or {})

    async def get(self, key: str) -> Any:
        return self.stored.get(key)

    async def set(self, key: str, value: Any) -> bool:
        self.stored[key] = value
        return True


class Session:
    """Enough of `ft.Page` to build the app against, plus the diff.

    `run_task` records rather than runs: there is no loop during a
    stateful test, and which of the app's own follow-up tasks get run --
    and when -- is something the test should decide, not something that
    happens behind it.
    """

    def __init__(self, route: str = "/", width: float = 1400.0) -> None:
        self.window = Window()
        self.route = route
        self.width = width
        self.height = 900.0
        self.title = ""
        self.padding: Any = 0
        self.bgcolor: str | None = None
        self.theme: ft.Theme | None = None
        self.dark_theme: ft.Theme | None = None
        self.theme_mode = ft.ThemeMode.LIGHT
        self.platform_brightness = ft.Brightness.LIGHT
        self.shared_preferences = FakePreferences()
        self.root: ft.Control | None = None
        self.tasks: list[tuple[Any, tuple[Any, ...]]] = []
        self.pushed: list[str] = []
        self.updates = 0
        self.flushes = 0
        self.on_route_change: Any = None
        self.on_resize: Any = None
        self.on_platform_brightness_change: Any = None

    # -- the parts the app calls -------------------------------------------

    def add(self, control: ft.Control) -> None:
        self.root = control

    def update(self, *_controls: Any) -> None:
        self.updates += 1

    def push(self, route: str) -> None:
        """A history entry, a new route, and the event the browser fires."""
        self.pushed.append(route)
        self.route = route
        if self.on_route_change is not None:
            self.on_route_change(RouteEvent(route))

    async def push_route(self, route: str) -> None:
        """Flet's own, which the app reaches through `run_task`."""
        self.push(route)

    def run_task(self, handler: Any, *args: Any) -> None:
        # Navigation happens here rather than at the next `pump()`: the
        # state machine's transitions are written around a route change
        # landing before the next one is asked for, which is what the
        # sync `page.go` used to give them.
        if getattr(handler, "__name__", "") == "push_route":
            self.push(*args)
            return
        self.tasks.append((handler, args))

    # -- the part a real client causes -------------------------------------

    def flush(self) -> None:
        """One round trip: diff the tree, then serialise what was added.

        This is where controls get frozen, so a test that never flushes is
        testing a world that does not exist.
        """
        if self.root is None:
            return
        self.flushes += 1
        _patch, added, _removed = ObjectPatch.from_diff(
            self.root, self.root, control_cls=BaseControl
        )
        for control in added:
            _serialise(control)

    def baseline(self) -> None:
        """Serialise the whole tree once, as the first render does."""
        _serialise(self.root)


class RouteEvent:
    def __init__(self, route: str) -> None:
        self.route = route
        self.data = route


class Point:
    """`Offset`-shaped, for the gesture events that carry one."""

    def __init__(self, x: float = 0.0, y: float = 0.0) -> None:
        self.x = x
        self.y = y


class Event:
    """A stand-in for the several event classes Flet actually sends.

    A fuzzer fires handlers it knows nothing about, so this carries the
    union of the fields those handlers read, with **type-correct**
    defaults. That matters more than it sounds: an earlier version
    answered `None` to anything it had not thought of, and a canvas
    resize handler then built a plot of `None x None` and raised -- a
    failure in this file wearing the app's traceback. Events that cannot
    happen do not prove anything.

    Anything still unmodelled reads as None, which is a loud enough
    failure to point back here rather than at the app.
    """

    def __init__(
        self,
        control: Any = None,
        data: Any = None,
        pixels: float = 0.0,
        max_scroll_extent: float = 0.0,
        width: float = 900.0,
        height: float = 400.0,
    ) -> None:
        self.control = control
        self.data = data
        self.name = "event"
        # A scrollable, at the end of its extent.
        self.pixels = pixels
        self.max_scroll_extent = max_scroll_extent
        # A canvas, with a size a chart can be drawn in.
        self.width = width
        self.height = height
        # A pointer, somewhere inside it.
        self.local_position = Point(width / 2, height / 2)
        self.global_position = Point(width / 2, height / 2)
        self.local_delta = Point(1.0, 1.0)
        self.scroll_delta = Point(0.0, 1.0)

    def __getattr__(self, _name: str) -> None:
        return None
