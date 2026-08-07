"""Token and chain marks, including the overlapping stack Curve uses.

A pool is drawn as its coins' logos overlapping left-to-right, the way
Curve's own list does it -- and for a metapool that means the *underlying*
assets, not the base pool's LP token, which is why this takes
`pool.display_coins` rather than `pool.coins`.

Every logo degrades: plenty of tokens have no image upstream, so a missing
one becomes a lettered disc in a colour derived from the symbol. That is
not only a fallback for a skipped `tools/build_assets.py` -- it is the
normal case for long-tail tokens.
"""

from __future__ import annotations

import flet as ft

from curve.models import Coin, Pool

from .assets import chain_logo, token_logo

#: How a logo gets from 200-280px of source art down to the 22-34px it is
#: drawn at. Two knobs, and the answer is not the intuitive one.
#:
#: Flutter's filter qualities are `none` (nearest), `low` (bilinear),
#: `medium` (bilinear **plus mipmaps**) and `high` (bicubic). Bicubic is a
#: *magnification* filter: reducing an image tenfold with it still reads a
#: few source pixels per output pixel, so fine artwork -- a torus of
#: hairlines, letters inside a disc -- comes out noisy. Mipmaps are what
#: minification wants, because each level is an average of the one above,
#: and `medium` is the only quality that uses them.
#:
#: The other knob is `cache_width`, which decodes the file at a chosen
#: size. It was set to three times the display size on the theory that
#: the decoder resamples better than the GPU. It does -- but it also
#: throws away the resolution the mipmap chain is built from, and the
#: result was worse than leaving it alone. Compared side by side at 27px
#: against the real assets, full-resolution decode with `medium` was the
#: only combination that looked right.
SAMPLING = ft.FilterQuality.MEDIUM


#: How much of each logo the next one covers. Enough to read as a group,
#: little enough that four coins are still four distinguishable discs.
OVERLAP = 0.34

#: Colours for the lettered fallback. Picked per symbol so a token looks the
#: same everywhere it appears, and from the Material palette so both themes
#: stay legible.
_FALLBACK_COLORS = (
    ft.Colors.BLUE_400,
    ft.Colors.TEAL_400,
    ft.Colors.PURPLE_300,
    ft.Colors.ORANGE_400,
    ft.Colors.PINK_300,
    ft.Colors.INDIGO_300,
    ft.Colors.CYAN_400,
    ft.Colors.AMBER_400,
)


def fallback_color(symbol: str) -> str:
    """A stable colour for a symbol, so the same token always matches."""
    if not symbol:
        return _FALLBACK_COLORS[0]
    return _FALLBACK_COLORS[sum(symbol.encode()) % len(_FALLBACK_COLORS)]


def initials_mark(symbol: str, size: float) -> ft.Container:
    """What stands in for a logo there is no image for.

    Quiet on purpose. This used to be two white letters on a saturated
    disc, which next to real token logos read as a brand rather than as an
    absence -- most obvious in the swap pickers, where one of the two coins
    would shout and the other would not. So the hue now only tints: a wash
    of it behind the letters and a hairline around them, with the text in
    the theme's own colour so it stays legible in both themes.

    The hue is still derived from the symbol, because several of these can
    sit side by side in one pool's stack and they have to be told apart.
    """
    accent = fallback_color(symbol)
    return ft.Container(
        ft.Text(
            (symbol or "?")[:3].upper(),
            size=size * 0.32,
            weight=ft.FontWeight.W_600,
            color=ft.Colors.ON_SURFACE_VARIANT,
            text_align=ft.TextAlign.CENTER,
            no_wrap=True,
        ),
        width=size,
        height=size,
        bgcolor=ft.Colors.with_opacity(0.18, accent),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.45, accent)),
        # Not `size / 2`. A border sits *outside* the box, so half the
        # content width no longer reaches the corners of the box it has
        # to round -- which draws a rounded square. Anything at or past
        # the half-width of the outer box is clamped to a circle.
        border_radius=size,
        alignment=ft.Alignment.CENTER,
    )


def token_mark(coin: Coin, chain: str, size: float = 24) -> ft.Container:
    """One coin's logo, or its initials when there is no image.

    Always a `Container` with an explicit size, so it can be dropped
    straight into a `Stack` with `left`/`top` and needs no extra wrapper.
    """
    source = token_logo(chain, coin.address)
    letters = initials_mark(coin.symbol, size)
    content: ft.Control
    if source:
        content = ft.Image(
            src=source,
            width=size,
            height=size,
            fit=ft.BoxFit.COVER,
            filter_quality=SAMPLING,
            # A logo that 404s falls back rather than leaving a hole. The
            # compiled subset can lag the API, and plenty of long-tail
            # tokens have no image upstream at all.
            error_content=letters,
        )
    else:
        content = letters

    return ft.Container(
        content,
        width=size,
        height=size,
        border_radius=size / 2,
        alignment=ft.Alignment.CENTER,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        tooltip=coin.symbol,
    )


def coin_stack(
    coins: list[Coin], chain: str, size: float = 24, limit: int = 5
) -> ft.Control:
    """Overlapping logos for a pool's assets, as Curve draws them.

    A `Stack` with explicit offsets, because Flet has no notion of negative
    spacing in a `Row`. The wrapper's width is computed rather than left to
    the Stack: a Stack sizes to its largest child, so without it every pool
    would occupy one logo's width and overlap the next column.
    """
    shown = coins[:limit]
    if not shown:
        return ft.Container(width=0, height=size)

    step = size * (1 - OVERLAP)
    marks: list[ft.Control] = []
    for index, coin in enumerate(shown):
        mark = token_mark(coin, chain, size)
        mark.left = index * step
        mark.top = 0
        # **No ring.** It used to draw one in the surface colour to
        # separate each disc from the one it covers, and it cost the
        # circle its shape: a border sits *outside* the box, so a 27px
        # mark with a 1.5px ring is a 30px box with a 13.5px radius --
        # a rounded square, which is exactly what crvUSD looked like.
        # The discs are their own separation.
        marks.append(mark)

    extra = len(coins) - len(shown)
    if extra > 0:
        marks.append(
            ft.Container(
                ft.Text(
                    f"+{extra}", size=size * 0.34, color=ft.Colors.ON_SURFACE_VARIANT
                ),
                left=len(shown) * step,
                top=0,
                width=size,
                height=size,
                bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
                border_radius=size / 2,
                alignment=ft.Alignment.CENTER,
            )
        )

    slots = len(shown) + (1 if extra > 0 else 0)
    return ft.Container(
        # **Painted right to left**, so each disc is *under* the one
        # before it rather than over it.
        #
        # A `Stack` paints in order, so the natural way round puts every
        # coin's left edge on top of its neighbour -- and a logo with
        # anything drawn in its top-left corner then floats that corner
        # in the middle of the seam. tacETH is the example: its artwork
        # carries an Ethereum badge up there, which landed on the WETH
        # disc beside it and read as a third coin in the pool.
        #
        # Reversed, a mark can only ever cover the one to its right, and
        # a corner badge disappears under its neighbour where it belongs.
        ft.Stack(list(reversed(marks)), width=step * (slots - 1) + size, height=size),
        width=step * (slots - 1) + size,
        height=size,
    )


def pool_stack(pool: Pool, size: float = 24, limit: int = 5) -> ft.Control:
    """The stack for a pool, decomposing a metapool into its underlying."""
    return coin_stack(pool.display_coins, pool.chain, size, limit)


def chain_mark(chain: str, size: float = 18) -> ft.Control | None:
    """A network's logo, or None when there is no image for it."""
    source = chain_logo(chain)
    if not source:
        return None
    return ft.Image(
        src=source,
        width=size,
        height=size,
        fit=ft.BoxFit.CONTAIN,
        filter_quality=SAMPLING,
    )
