"""Buttons that belong to the theme they are drawn in.

Material draws a button as a stadium with a tonal fill and a blurred
elevation shadow under it. Chad is a theme of boxes -- outlined, barely
rounded, casting a hard one-colour shadow (see `ui.theme`) -- and a
stadium among those reads as a control borrowed from another program. So
under Chad the buttons get the same treatment as everything else: small
corners, a solid edge, and the same flat shadow the panels use.

Outside Chad everything here is a no-op. Material's own button is right
for a Material palette, and an outline there only draws a line where
there was contrast already.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import flet as ft

from . import theme

#: Corner radius under Chad. Material's own is half the button's height,
#: which is a pill; four is about what the rest of this theme's corners
#: are cut to, and enough that the shadow behind it does not show a
#: square poking out from under a rounded one.
RADIUS = 4


def _over(top: str, bottom: str, alpha: float) -> str:
    """`top` at `alpha`, composited on `bottom`. Hex in, hex out."""
    a = round(int(top[1:3], 16) * alpha + int(bottom[1:3], 16) * (1 - alpha))
    b = round(int(top[3:5], 16) * alpha + int(bottom[3:5], 16) * (1 - alpha))
    c = round(int(top[5:7], 16) * alpha + int(bottom[5:7], 16) * (1 - alpha))
    return f"#{a:02X}{b:02X}{c:02X}"


#: What a disabled button is made of, blended here rather than left to
#: Material -- and this is the one genuinely surprising thing in the file.
#:
#: Giving a Flet button *any* `ButtonStyle` costs it its disabled colours:
#: a style with nothing in it but a corner radius still turns the fill
#: slate (#8D8E8E under this palette, against Material's #D8D9D7), which
#: on a Deposit button -- disabled for as long as the amount box is empty,
#: which is most of the time -- looks like an error state.
#:
#: Naming the state does fix it, but only with a *literal* colour.
#: `with_opacity(0.12, ON_SURFACE)` inside the state map resolves to
#: nothing and Flutter falls back to the same slate, so the blend Material
#: would have done is done above: the body colour at 12% for the fill and
#: at 38% for the label, which is Material's own recipe, spelled out.
#:
#: Blended over the *panel*, not over `RAISED`: Material's disabled fill
#: is translucent, so what shows through it is whatever the button is
#: lying on -- and every button here lies on a panel.
DISABLED_FILL = _over(theme.INK, theme.PANEL, 0.12)
DISABLED_TEXT = _over(theme.INK, DISABLED_FILL, 0.38)


def style(page: ft.Page) -> ft.ButtonStyle | None:
    """How a button is drawn under Chad. None -- Material's own -- elsewhere.

    Three departures. The corners. A solid outline in the palette's
    *border* grey rather than the hairline the panels take: a panel is
    something to read and a button is something to press, and the edge is
    what says where to press -- quietened to that hairline once it cannot
    be pressed. And no elevation, because the shadow comes from `shadowed`
    below instead, and Material's own would be a second one, blurred,
    which is the thing this theme exists to not have.

    The enabled colours are what Material was giving it anyway; they are
    named because a state map with a gap in it draws the missing state as
    nothing at all, which is a button you can see straight through.
    """
    if not theme.is_chad(page):
        return None
    return ft.ButtonStyle(
        shape=ft.RoundedRectangleBorder(radius=RADIUS),
        side={
            ft.ControlState.DEFAULT: ft.BorderSide(1, ft.Colors.OUTLINE),
            ft.ControlState.DISABLED: ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
        },
        elevation=0,
        bgcolor={
            ft.ControlState.DEFAULT: ft.Colors.SURFACE_CONTAINER_LOW,
            ft.ControlState.DISABLED: DISABLED_FILL,
        },
        color={
            ft.ControlState.DEFAULT: ft.Colors.PRIMARY,
            ft.ControlState.DISABLED: DISABLED_TEXT,
        },
    )


class Shadowed(ft.Container):
    """A hard shadow behind a button, and nothing else.

    A wrapper because it has to be: `ft.Button` has no shadow of its own
    beyond Material's elevation, and `ft.ButtonStyle` offers no way to
    make that one flat.

    Two things it does on its own, both because the button it wraps is
    handled from a dozen places that know nothing about it:

      * **visibility.** Flet skips an invisible control entirely, so a
        hidden button costs no space -- but a *wrapper* around one is
        still a child of the column and still takes its 12px of spacing.
        Hiding the approve step would leave a gap where it had been. The
        wrapper reads the button's own `visible` back at update time
        rather than asking every caller to set two things.
      * **the theme.** The header is built once and outlives any number
        of theme switches, so the shadow is recomputed rather than fixed
        at construction. `theme.is_chad` reads the live page, which is
        why holding the page is enough.
    """

    def __init__(
        self,
        button: ft.Control,
        page: ft.Page,
        when: Callable[[], bool] | None = None,
    ) -> None:
        self._page = page
        #: An extra condition on top of the button's own -- "and there is
        #: room for its label". Kept here rather than in the caller so the
        #: wrapper stays the single answer to "is this button showing":
        #: a container that is visible around a button that is not still
        #: takes its share of the row's spacing.
        self._when = when
        super().__init__(button, border_radius=RADIUS)

    def before_update(self) -> None:
        super().before_update()
        showing = self.content is not None and self.content.visible
        self.visible = showing and (self._when is None or self._when())
        self.shadow = theme.panel_shadow(self._page, inset=True)


def shadowed(
    button: ft.Control, page: ft.Page, when: Callable[[], bool] | None = None
) -> ft.Control:
    """`button`, with room for Chad's shadow under it.

    Always wrapped, even outside Chad -- a container with no shadow and
    no fill is exactly the size of what it holds -- so that switching
    theme has something to put a shadow on without rebuilding the tree.
    """
    return Shadowed(button, page, when)


class StandIn(ft.IconButton):
    """The same action as `button`, with the label dropped.

    A phone header has room for about two words in total, so the labelled
    buttons give way to their icons. This is a second control rather than
    the same one with its text removed because `ft.Button` will not render
    with an `icon` and no `content` at all -- which is also why the connect
    button says "Connecting..." rather than going quiet.

    It follows the button it stands for instead of being told: `visible`
    and `disabled` are set on that one from a dozen places that know
    nothing about layout, and two things to keep in step is one thing to
    forget.
    """

    def __init__(
        self, button: ft.Control, when: Callable[[], bool], **kwargs: Any
    ) -> None:
        self._button = button
        self._when = when
        super().__init__(**kwargs)

    def before_update(self) -> None:
        super().before_update()
        self.visible = bool(self._button.visible and self._when())
        self.disabled = bool(self._button.disabled)
        # And its words, where it has any. The label is where the action
        # says what it is doing -- "Connecting..." rather than "Connect
        # wallet" -- and once the labelled button is not drawn at all,
        # that sentence has nowhere else to go. On the icon it becomes the
        # tooltip, which is the only place an icon can say anything.
        label = getattr(self._button, "content", None)
        if isinstance(label, str) and label:
            self.tooltip = label
