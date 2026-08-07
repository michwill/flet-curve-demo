"""Three themes: light, dark, and Chad.

The first two are Material's, seeded from one colour and left to work out
the rest. **Chad** is not: it is a hand-set palette lifted from
linux.org.ru's default stylesheet, which is a particular kind of yellowed
grey that Material's generator will not produce from any seed.

The colours below are that site's `:root` block, taken from
`waltz/combined.css` and mapped onto Material's slots by role rather than
by name -- what a colour *does* there is what it does here:

    --main-background      #ECECEC   the page behind everything
    --article-background   #FFF      panels, rows, dialogs
    --table-hover-background #FFE9C0 the yellow a row goes when hovered
    --icon-button-active-color #c17d11 an active control
    --tagpage-group-label-background #E7AF55 a label that wants noticing
    --tag-color            #77521D   secondary text with a brown cast
    --article-border-color #808080   a real border
    --table-border-color   #CCC      a rule between rows
    --link-color           #275096   the one blue in the place
    --targeted-message-border-color #a00  an error

Two Material slots have no equivalent and are derived: `surface_tint`
(Material blends it into elevated surfaces, so it takes the amber) and
`inverse_*` (used by snackbars, which this app does not raise).

**Shadows.** The other half of the look. Material's elevation draws a
blurred gradient; this theme draws a hard offset instead -- one colour,
one edge, no blur -- which is what the shadow under a bordered box looked
like before shadows became soft. `PANEL_SHADOW` is that, and it is only
used under Chad: the same shadow under a Material surface would look like
a mistake.
"""

from __future__ import annotations

import flet as ft

#: The seed the Material themes are generated from.
SEED = ft.Colors.INDIGO

# -- Chad's palette, from linux.org.ru ------------------------------------

PAGE = "#ECECEC"
PANEL = "#FFFFFF"
HOVER = "#FFE9C0"
AMBER = "#E7AF55"
ACTIVE = "#C17D11"
BROWN = "#77521D"
BORDER = "#808080"
RULE = "#CCCCCC"
LINK = "#275096"
DANGER = "#AA0000"
INK = "#000000"
QUIET = "#444444"
POSITIVE = "#447019"

#: A hard shadow: no blur, no spread, one constant opacity. Material's own
#: elevation would put a gradient here, which is exactly the thing this
#: theme is not.
PANEL_SHADOW = ft.BoxShadow(
    spread_radius=0,
    blur_radius=0,
    offset=ft.Offset(3, 3),
    color=ft.Colors.with_opacity(0.18, INK),
)

#: The same, smaller, for things that sit inside a panel.
INSET_SHADOW = ft.BoxShadow(
    spread_radius=0,
    blur_radius=0,
    offset=ft.Offset(2, 2),
    color=ft.Colors.with_opacity(0.14, INK),
)

#: Theme names as the app stores them, in the order the button cycles.
NAMES = ("light", "dark", "chad")


def material() -> ft.Theme:
    """Light or dark, generated from the seed as before.

    One theme object serves both: which of `page.theme` and
    `page.dark_theme` it is assigned to decides the brightness, so there
    is nothing to pass here.
    """
    return ft.Theme(color_scheme_seed=SEED)


def chad() -> ft.Theme:
    """The yellowed-grey theme, spelled out slot by slot.

    Every value is from the stylesheet above except where Material asks
    for something that page has no equivalent of, and those are noted.
    """
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            # An active control, and the thing the eye should land on.
            primary=ACTIVE,
            on_primary=PANEL,
            primary_container=HOVER,
            on_primary_container=BROWN,
            # A label that wants noticing without being a control.
            secondary=AMBER,
            on_secondary=INK,
            secondary_container=HOVER,
            on_secondary_container=BROWN,
            # The one blue in the place: links, and the wrong-network note.
            tertiary=LINK,
            on_tertiary=PANEL,
            tertiary_container="#D8E2F2",
            on_tertiary_container=LINK,
            error=DANGER,
            on_error=PANEL,
            error_container="#FFC8BD",  # the stylesheet's own soft red
            on_error_container=DANGER,
            # Panels are white; the page behind them is the grey.
            surface=PANEL,
            on_surface=INK,
            on_surface_variant=QUIET,
            surface_bright=PANEL,
            surface_dim="#DCDCDC",
            surface_container_lowest=PANEL,
            surface_container_low="#F6F6F6",
            surface_container=PAGE,
            surface_container_high="#E4E4E4",
            surface_container_highest="#DCDCDC",
            # Material blends this into anything it considers elevated.
            # Amber, so elevation warms rather than tinting toward blue.
            surface_tint=AMBER,
            outline=BORDER,
            outline_variant=RULE,
            shadow=INK,
            scrim=INK,
            inverse_surface="#2B2B2B",
            on_inverse_surface="#F0F0F0",
            inverse_primary=AMBER,
        ),
    )


def theme_for(name: str) -> tuple[ft.Theme, ft.ThemeMode]:
    """The theme and the mode that goes with it.

    Chad is a light theme with its own colours, so it rides in `theme`
    with the mode pinned to light -- leaving the mode on SYSTEM would let
    a dark desktop swap in `dark_theme` behind its back.
    """
    if name == "chad":
        return chad(), ft.ThemeMode.LIGHT
    if name == "dark":
        return material(), ft.ThemeMode.DARK
    return material(), ft.ThemeMode.LIGHT


def is_chad(page: ft.Page) -> bool:
    """Is the Chad theme the one on screen?

    Read from the page rather than tracked separately, so a control built
    at any time asks the same question and gets the current answer.

    Anything that cannot answer -- a page with no theme set yet, or a test
    stub standing in for one -- is not Chad, which is the useful default:
    the shadows stay off.
    """
    scheme = getattr(getattr(page, "theme", None), "color_scheme", None)
    return bool(scheme and scheme.primary == ACTIVE)


def panel_shadow(page: ft.Page, *, inset: bool = False) -> ft.BoxShadow | None:
    """The hard shadow, under Chad only. None elsewhere."""
    if not is_chad(page):
        return None
    return INSET_SHADOW if inset else PANEL_SHADOW
