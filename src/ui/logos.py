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

from .assets import MARK_PIXELS, chain_logo, token_logo

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

#: What the browser needed on top, and no longer does.
#:
#: `cache_width` used to be set here, to twice the size a mark is drawn
#: at. It was worth having when the art was 200-280px: CanvasKit builds
#: no mipmap chain, so a tenfold reduction aliased whatever filter it was
#: given, and decoding smaller left less for the filter to do.
#:
#: It is gone for two reasons, in that order:
#:
#:   * `build_assets.py` now compiles every mark to `MARK_PIXELS`, so the
#:     reduction left at runtime is small and the filter copes;
#:   * **on WebKit it was the bug.** Decoding to a size goes through the
#:     browser, and WebKit's resize path gets premultiplication wrong: it
#:     put a pale rim around every mark that had one, on iOS only, because
#:     Chrome and Firefox decode by other routes. Reproduced in WebKitGTK
#:     at a device pixel ratio of 3 -- the rim measures 21 levels brighter
#:     than the header behind it with the hint, and exactly the background
#:     without it. A ratio of 1 shows nothing either way, which is why
#:     every desktop window looked right.
#:
#: So nothing here asks the browser to resize anything -- and nothing can
#: any more even if it wanted to: on Flet 0.86.5 a `cache_width` on a web
#: build renders the image as *nothing at all*, silently, with no
#: exception raised. Restored and measured: every token mark, chain mark
#: and wallet icon disappeared while the files still served 200. So that
#: knob is not a thing to go back to; it is gone for good.
#:
#: What replaces it is `ui.assets.MARK_TIERS`: the file arrives at very
#: nearly the size it is drawn because the *build* wrote one that size,
#: and this picks it. Same goal as `cache_width` -- leave the renderer
#: almost nothing to minify -- reached without asking the browser to
#: resize anything, which is what put a pale rim on WebKit.

#: How many device pixels the platform draws per logical one, which is
#: what turns a mark's drawn size into the tier to ask for.
#:
#: This was missed the first time round and the mistake is worth keeping:
#: a ratio of 1 is the only one where the logical size and the physical
#: size cannot be told apart, and every desktop window is a ratio of 1 --
#: so a bug that only exists above 1 looks like no bug at all from a
#: desktop. Phones report 2, 3, and 3.5.
#:
#: Asking the platform, not assuming. `page.media` carries it and
#: `CurveApp._apply_layout` hands it over -- read there rather than once
#: at startup, because `page.media` is not always answered by the first
#: paint and a window moved between displays changes it. The default is 2
#: rather than 1 so a mark built before the page has answered errs
#: towards too much resolution rather than too little. Held in a list so
#: nothing here needs `global`, and so a reader can see there is exactly
#: one of it.
_pixel_ratio = [2.0]


def set_pixel_ratio(ratio: float | None) -> None:
    """Tell the marks how many device pixels a logical one is worth.

    Ignores nothing-yet: the standing guess is better than multiplying by
    None.
    """
    if ratio and ratio > 0:
        _pixel_ratio[0] = float(ratio)


def pixel_ratio() -> float:
    """What the platform last said, or the standing guess."""
    return _pixel_ratio[0]


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


#: How many times a mark is asked for before it settles for its fallback.
#:
#: Two, and the second attempt is the whole of a real bug fix rather than
#: optimism. The marks are deliberately not warmed when the site is
#: published -- 6,716 files, see `LAZY_DIR` in `tools/publish_ipfs.py` --
#: so each one is retrieved *cold* by whichever visitor looks first, and an
#: IPFS gateway that cannot find a block inside its retrieval budget
#: answers 504 after about seventeen seconds.
#:
#: Measured against curve.eth.limo rather than reasoned about: 2 of the 25
#: Gnosis marks failed that way on one pass, a different one failed in a
#: browser a minute earlier, and all three served in under two seconds when
#: asked again. Which marks fail is a property of what is cold at that edge
#: at that moment, which is why the same page is missing different logos
#: for different people -- and why it had looked like a bug in the build.
#:
#: The first request is itself what warms the block, so asking twice is
#: close to asking once and then succeeding.
MARK_ATTEMPTS = 2


def _attempt_src(source: str, attempt: int) -> str:
    """The URL for one attempt. Attempt 0 is the file's own address.

    Later attempts add a query string, which the gateway ignores -- checked
    against curve.eth.limo, which serves the identical 2,250 bytes with and
    without it. It is not there for the server. Flutter's `ImageCache` is
    keyed by URL and **caches failures**, so asking for the same string
    again is answered from that cache without a request ever being made,
    which is precisely why a mark that fails once stays blank for the rest
    of the session. A different key is a different entry, and a real
    request.
    """
    return source if attempt == 0 else f"{source}?retry={attempt}"


def _mark_image(
    source: str, size: float, fallback: ft.Control, fit: ft.BoxFit
) -> ft.Control:
    """A mark, with each retry nested in the previous attempt's failure slot.

    `error_content` is the only failure hook Flet's `Image` has -- there is
    no `on_error` to hang a callback on, and `error_content` is a passive
    Control rather than an event. But it is a Control that Flutter builds
    *at the moment the load fails*, so putting an `Image` in there is a
    retry that costs nothing until it is needed: no request is made, and no
    time is spent, except by the marks that actually failed.
    """
    content = fallback
    for attempt in reversed(range(MARK_ATTEMPTS)):
        content = ft.Image(
            src=_attempt_src(source, attempt),
            width=size,
            height=size,
            fit=fit,
            filter_quality=SAMPLING,
            error_content=content,
        )
    return content


def token_mark(coin: Coin, chain: str, size: float = 24) -> ft.Container:
    """One coin's logo, or its initials when there is no image.

    Always a `Container` with an explicit size, so it can be dropped
    straight into a `Stack` with `left`/`top` and needs no extra wrapper.

    The initials are the last resort rather than the first answer to a
    failed load: a mark that 404s has no image upstream and lettering is
    correct, but one that times out has an image and simply was not
    reached. See `MARK_ATTEMPTS`.
    """
    source = token_logo(chain, coin.address, size * pixel_ratio())
    letters = initials_mark(coin.symbol, size)
    content: ft.Control = (
        _mark_image(source, size, letters, ft.BoxFit.COVER) if source else letters
    )

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


def chain_mark(
    chain: str, size: float = 18, *, sized_by_parent: bool = False
) -> ft.Control | None:
    """A network's logo, or None when there is no image for it.

    A bare `Image`, deliberately, where `token_mark` is a clipped
    `Container`. Wrapping this one to clip the disc was tried and made it
    worse: as the picker's `leading_icon` it goes into a decoration box
    that stretches it to the field's height, and a `Container` told to
    clip then cropped a 14-wide, 24-tall pill out of a round mark. An
    `Image` with both sides set and `CONTAIN` keeps its shape wherever it
    is put, which is the property that matters more here than the clip.

    **`sized_by_parent` is the other half of that same stretch**, and it
    was missed when the marks went tiered. That decoration box means
    `size` is what this control is *given*, not what it ends up drawn at
    -- so choosing a tier from it asks for art the field then magnifies.
    The picker's mark is handed 14 and drawn at the field's height, which
    fetched the 20px tier for something nearer 27 device pixels: the one
    mark on the page that still looked like a low-resolution copy, while
    every token beside it had come good.

    Nothing here measures the field. This app does not compute layout
    sizes, and a number derived from Material's own metrics would be
    wrong the first time either changed. It simply stops guessing low and
    takes the top tier, which costs one extra file for one mark -- the
    selected network's, once.
    """
    wanted = MARK_PIXELS if sized_by_parent else size * pixel_ratio()
    source = chain_logo(chain, wanted)
    if not source:
        return None
    # The same retry as a token's, for the same reason -- this one is
    # fetched from the same gateway and goes cold the same way. There is no
    # lettered stand-in for a network, so the last resort is the empty box
    # that a failure would have left anyway.
    return _mark_image(source, size, ft.Container(width=size, height=size), ft.BoxFit.CONTAIN)
