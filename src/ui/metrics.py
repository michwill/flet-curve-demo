"""How tall text actually is here.

One number in this app is a guess at a height in pixels: how tall the
action panel has to be, because `TabBarView` cannot size to its content
(see `pool_detail.actions_height`). Every other absolute size is a width
or a gap, and those merely look wrong when they are wrong. That one
decides whether the Deposit button is on screen.

The guess was measured against this machine, and a platform whose text
comes out taller -- an accessibility font scale, iOS Dynamic Type, a
different default font -- would push the panel's content past it. Flet
exposes nothing to scale by: `PageMediaData` has the device pixel ratio,
the padding and the orientation, and no text scale factor.

So it is measured rather than assumed. A single line of body text is put
on the page, its rendered height is reported back, and everything derived
from a line of text is scaled by how far that is from what it is here.
On this machine the factor is 1 and nothing changes; at a 1.3 text scale
the panel grows by about a third, which is what its content did.

The probe is a real control and has to be in the tree to be measured. It
is one line tall and invisible, which is cheap enough to leave in the
header for the life of the app -- and it re-reports if the platform's
metrics change under it, which is what happens when someone moves the
window to a display with different scaling.
"""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

from .typography import BODY

#: What one line of body text measures here, at `typography.BODY`.
#: Measured, not assumed: an `ft.Text("Ag", size=15)` reports 21.0 on this
#: machine, and everything in `pool_detail.ACTIONS_*` was measured
#: alongside it. Twenty would have been close enough to look right and
#: still scaled every panel by 5% on the platform the numbers came from.
LINE_HEIGHT = 21.0

#: How far this platform is from that. 1.0 until the probe reports.
#:
#: Module state because it is genuinely global -- a property of the
#: platform, not of any control -- and because the alternative is
#: threading a float through every view that lays anything out by hand.
#: A holder rather than a bare name so that reading it is always current:
#: a module-level float would be copied by `from ... import SCALE` at the
#: moment of import, which is before the probe has said anything.
@dataclass
class _Platform:
    scale: float = 1.0


_PLATFORM = _Platform()

#: Sanity bounds. A report outside these is not a text scale, it is a
#: measurement taken before layout settled, and scaling a panel by it
#: would be worse than not scaling at all.
MIN_SCALE = 0.5
MAX_SCALE = 3.0


def scale() -> float:
    """How much taller text is here than where these numbers came from."""
    return _PLATFORM.scale


def scaled(value: float) -> float:
    """`value`, in this platform's text metrics rather than in those."""
    return value * _PLATFORM.scale


def _measured(event: ft.Event) -> None:
    height = getattr(event, "height", None)
    if not isinstance(height, int | float) or height <= 0:
        return
    factor = float(height) / LINE_HEIGHT
    if MIN_SCALE <= factor <= MAX_SCALE:
        _PLATFORM.scale = factor


def probe() -> ft.Control:
    """One line of body text, measured. Put it in the tree once."""
    return ft.Container(
        ft.Text("Ag", size=BODY, no_wrap=True),
        # Invisible, but *not* `visible=False`: Flet does not build a
        # control that is not visible, and one that is not built is never
        # measured. Zero opacity is drawn, and drawn is measured.
        opacity=0.0,
        on_size_change=_measured,
        # It reports on the way in, and again if the platform changes its
        # metrics under a running app -- moving a window to a display with
        # different scaling.
        size_change_interval=500,
    )


def reset() -> None:
    """Back to unscaled. For tests, which share module state."""
    _PLATFORM.scale = 1.0
