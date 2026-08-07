"""Three themes: light, dark, and Chad.

The first two are Material's, seeded from one colour and left to work out
the rest. **Chad** is not: it is a hand-set palette taken from
linux.org.ru's default look, which is the Tango palette -- warm greys with
chocolate, butter and orange accents -- and which Material's generator
will not produce from any seed.

**Where these numbers come from.** The site ships several stylesheets and
the default one is `tango/combined.css`, not the `waltz` sheet linked from
the settings page; and within tango the `:root` block appears twice, dark
first and light second. An earlier version of this file took the waltz
values, which are a different palette wearing the same variable names --
that theme's row highlight is a pale amber, and the real one is plum.
These were read off the live page instead (`getComputedStyle` on
`document.documentElement` at `/forum/talks/`), which is the only way to
be sure which sheet and which block actually win:

    --main-background                #D3D7CF   the page behind everything
    --article-background             #EEEEEC   panels, boxes, dialogs
    --text-color                     #3B4245   body text
    --header-color                   #232829   headings
    --table-link-color               #171B1C   a table's own links
    --blockquote-color               #555753   quieter text
    --table-border-color             #BABDB6   the rule between rows
    --table-hover-background         #AD7FA8   the row under the pointer
    --icon-button-active-color       #C17D11   an active control
    --tagpage-group-label-background #E9B96E   a label that wants noticing
    --main-menu-color                #8F5902   the navigation
    --tag-color                      #CE5C00   tags
    --link-color                     #204A87   an ordinary link
    --button-primary-background      #729FCF   a primary button
    --signature-user-color           #4E9A06   a name, i.e. something good
    --button-danger-background       #CC0000   something dangerous

Mapped onto Material's slots by role rather than by name -- what a colour
*does* there is what it does here. Aluminium 4 (`#888A85`) is the one
addition: tango leaves `--article-border-color` undefined, so the border
around a box falls back to the text colour, and a real border colour from
the same palette is closer to how it looks than `#3B4245` would be.

Two Material slots have no counterpart at all and are derived:
`surface_tint` (Material blends it into elevated surfaces, so it takes the
butter) and `error_container` (nothing on that page is a soft red).

**Shadows.** The other half of the look. Material's elevation draws a
blurred gradient; this theme draws a hard offset instead -- one colour,
one edge, no blur -- which is what the shadow under a bordered box looked
like before shadows became soft. `PANEL_SHADOW` is that, and it is only
used under Chad: the same shadow under a Material surface would look like
a mistake. (linux.org.ru itself has no shadows at all -- `box-shadow` is
`none` everywhere on that page. These are the requested addition, in the
spirit of the rest.)
"""

from __future__ import annotations

import flet as ft

#: The seed the Material themes are generated from.
SEED = ft.Colors.INDIGO

# -- Chad's palette: Tango, as linux.org.ru uses it ------------------------

PAGE = "#D3D7CF"       # Aluminium 2  --main-background
PANEL = "#EEEEEC"      # Aluminium 1  --article-background
INK = "#3B4245"        #              --text-color
HEADING = "#232829"    #              --header-color
TITLE = "#171B1C"      #              --table-link-color
QUIET = "#555753"      # Aluminium 5  --blockquote-color
RULE = "#BABDB6"       # Aluminium 3  --table-border-color
BORDER = "#888A85"     # Aluminium 4  (tango leaves this one undefined)
HOVER = "#AD7FA8"      # Plum 1       --table-hover-background
ACTIVE = "#C17D11"     # Chocolate 2  --icon-button-active-color
LABEL = "#E9B96E"      # Chocolate 1  --tagpage-group-label-background
BROWN = "#8F5902"      # Chocolate 3  --main-menu-color
ORANGE = "#CE5C00"     # Orange 3     --tag-color
LINK = "#204A87"       # Sky Blue 3   --link-color
SKY = "#729FCF"        # Sky Blue 1   --button-primary-background
GREEN = "#4E9A06"      # Chameleon 3  --signature-user-color
DANGER = "#CC0000"     # Scarlet 2    --button-danger-background

#: A hard shadow: no blur, no spread, one constant opacity. Material's own
#: elevation would put a gradient here, which is exactly the thing this
#: theme is not. Cast in the palette's near-black rather than pure black,
#: which goes blue against these warm greys.
PANEL_SHADOW = ft.BoxShadow(
    spread_radius=0,
    blur_radius=0,
    offset=ft.Offset(3, 3),
    color=ft.Colors.with_opacity(0.20, TITLE),
)

#: The same, smaller, for things that sit inside a panel.
INSET_SHADOW = ft.BoxShadow(
    spread_radius=0,
    blur_radius=0,
    offset=ft.Offset(2, 2),
    color=ft.Colors.with_opacity(0.16, TITLE),
)

#: For the top bar, which spans the window: straight down, with no
#: sideways offset. A bar that reaches both edges has no right-hand edge
#: to cast from, and 3px of shadow hanging off the side of the window is
#: how you can tell a panel shadow was reused for one.
BAR_SHADOW = ft.BoxShadow(
    spread_radius=0,
    blur_radius=0,
    offset=ft.Offset(0, 3),
    color=ft.Colors.with_opacity(0.20, TITLE),
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
    """The Tango theme, spelled out slot by slot.

    Every value is from the palette above except where Material asks for
    something that page has no equivalent of, and those are noted.
    """
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            # An active control, and the thing the eye should land on.
            primary=ACTIVE,
            on_primary=PANEL,
            primary_container=LABEL,
            on_primary_container=BROWN,
            # A label that wants noticing without being a control. The
            # navigation brown, over the butter it sits on there.
            secondary=BROWN,
            on_secondary=PANEL,
            secondary_container=LABEL,
            on_secondary_container=TITLE,
            # Links, and the wrong-network notice, which is drawn as a
            # tint of this.
            tertiary=LINK,
            on_tertiary=PANEL,
            tertiary_container=SKY,
            on_tertiary_container=TITLE,
            error=DANGER,
            on_error=PANEL,
            # Derived: nothing on that page is a soft red.
            error_container="#EFC7C2",
            on_error_container=DANGER,
            # Panels are the light aluminium; the page behind them is the
            # darker one, which is the whole shape of the site.
            surface=PANEL,
            on_surface=INK,
            on_surface_variant=QUIET,
            surface_bright="#F6F6F4",
            surface_dim=PAGE,
            surface_container_lowest="#FFFFFF",
            surface_container_low="#F3F3F1",
            surface_container=PAGE,
            surface_container_high="#C9CDC6",
            surface_container_highest=RULE,
            # Material blends this into anything it considers elevated.
            # Butter, so elevation warms rather than tinting toward blue.
            surface_tint=LABEL,
            outline=BORDER,
            outline_variant=RULE,
            shadow=TITLE,
            scrim=TITLE,
            inverse_surface=INK,
            on_inverse_surface=PANEL,
            inverse_primary=LABEL,
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


def bar_shadow(page: ft.Page) -> ft.BoxShadow | None:
    """The top bar's shadow: straight down. None outside Chad."""
    return BAR_SHADOW if is_chad(page) else None


def border_side(page: ft.Page) -> ft.BorderSide | None:
    """A hairline outline, under Chad only.

    Chad is a theme of boxes -- everything on that page sits in a bordered
    one -- and the shadows it casts need an edge to come from. Material's
    own light and dark separate things by tone instead, and an outline
    there only adds a line where there was contrast already.
    """
    return ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT) if is_chad(page) else None


def panel_border(page: ft.Page) -> ft.Border | None:
    """The same outline, on all four sides. None outside Chad."""
    side = border_side(page)
    return ft.Border.all(side.width, side.color) if side else None


def rows_theme(page: ft.Page) -> ft.Theme | None:
    """A theme for the rows alone, colouring the hover plum.

    The plum is the single most recognisable thing about that site, and
    Material would never arrive at it: `hover_color` is what an `InkWell`
    paints on hover, and it defaults to a translucent tint of the surface.
    An opaque colour there covers the row.

    Doing it through the theme rather than the row is not a stylistic
    choice. A row carries `key="pool-row-N"`, and Flet **freezes** a keyed
    control when a rebuild matches an old one to a new one by key -- after
    which assigning to any of its properties raises "Frozen controls
    cannot be updated". An `on_hover` handler that set `bgcolor` therefore
    worked exactly until the first rebuild (a theme change, a resize) and
    then threw on the next hover. Nothing here touches a row at all.

    Nested and without a `theme_mode`, so it inherits the page's own theme
    and overrides this one value. None elsewhere: Material's ink overlay
    is right for a Material palette.
    """
    return ft.Theme(hover_color=HOVER) if is_chad(page) else None


def header_bg(page: ft.Page) -> str | None:
    """The strip behind a table's column headings.

    `#bd .forum table thead` takes the *border* colour there, which reads
    as a grey band above white rows. None elsewhere: Material tables have
    no band, and adding one would be this theme leaking.
    """
    return RULE if is_chad(page) else None
