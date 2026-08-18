"""Resolving compiled curve-assets paths, and the marks built from them.

These run against the real compiled output, so they also serve as a check
that `tools/build_assets.py` produced something usable. Where a test would
depend on a specific token existing upstream, it asserts the *shape* of the
answer instead.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import flet as ft
import pytest

from curve.models import Coin, Pool
from ui import logos
from ui.assets import ASSET_ROOT, chain_logo, chain_name, curve_logo, token_logo
from ui.logos import (
    OVERLAP,
    coin_stack,
    fallback_color,
    pool_stack,
    token_mark,
)

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


# -- chain names -----------------------------------------------------------


def test_chain_names_are_capitalised() -> None:
    assert chain_name("ethereum") == "Ethereum"
    assert chain_name("arbitrum") == "Arbitrum"
    assert chain_name("polygon") == "Polygon"


def test_chains_with_real_names_are_not_just_capitalised() -> None:
    """"bsc" is not a word, and "Xdai" is not what anyone calls it."""
    assert chain_name("bsc") == "BNB Chain"
    assert chain_name("xdai") == "Gnosis"
    assert chain_name("zksync") == "zkSync"


def test_hyphens_are_word_breaks() -> None:
    assert chain_name("x-layer") == "X Layer"


def test_chain_name_of_nothing_is_nothing() -> None:
    assert chain_name("") == ""


# -- paths -----------------------------------------------------------------


def test_a_known_token_resolves_under_the_asset_root() -> None:
    url = token_logo("ethereum", USDC)
    assert url and url.endswith(".png")
    assert ASSET_ROOT in url
    assert "ethereum" in url


def test_token_lookup_is_case_insensitive() -> None:
    """Upstream names files by lowercased address; the API checksums them."""
    assert token_logo("ethereum", USDC) == token_logo("ethereum", USDC.lower())


def test_a_token_with_no_image_returns_none() -> None:
    """The normal case for long-tail tokens, not an error."""
    assert token_logo("ethereum", "0x" + "de" * 20) is None


def test_missing_inputs_are_handled() -> None:
    assert token_logo("", USDC) is None
    assert token_logo("ethereum", "") is None
    assert chain_logo("") is None


def test_chain_and_curve_logos_resolve() -> None:
    assert chain_logo("ethereum")
    assert chain_logo("ETHEREUM") == chain_logo("ethereum")
    assert curve_logo()


def test_an_unknown_chain_has_no_logo() -> None:
    assert chain_logo("not-a-chain") is None


# -- marks -----------------------------------------------------------------


def coin(symbol: str, address: str = "0x" + "11" * 20) -> Coin:
    return Coin(address=address, symbol=symbol, decimals=18)


def test_a_token_with_an_image_renders_one() -> None:
    mark = token_mark(coin("USDC", USDC), "ethereum", 24)
    assert isinstance(mark.content, ft.Image)


def test_a_token_without_an_image_renders_its_initials() -> None:
    mark = token_mark(coin("WOOF"), "ethereum", 24)
    assert isinstance(mark.content, ft.Container)
    assert mark.content.content.value == "WOO"


def test_the_initials_mark_only_tints_with_its_hue() -> None:
    """It used to be white letters on a saturated disc, which read as a
    brand rather than as a missing logo -- loudest in the swap pickers,
    where one coin would shout and the other would not. The hue now tints
    the background and the border only; the letters take the theme's."""
    from ui.logos import initials_mark

    mark = initials_mark("WOOF", 24)
    assert mark.content.color == ft.Colors.ON_SURFACE_VARIANT
    assert "0.18" in str(mark.bgcolor) or "," in str(mark.bgcolor)
    assert mark.bgcolor != fallback_color("WOOF")


def last_resort(mark):
    """Walk a mark's retry chain to whatever it settles for."""
    node = mark.content
    while isinstance(node, ft.Image):
        node = node.error_content
    return node


def attempts(mark) -> list[str]:
    """The URLs a mark will ask for, in order."""
    urls, node = [], mark.content
    while isinstance(node, ft.Image):
        urls.append(node.src)
        node = node.error_content
    return urls


def test_a_missing_image_falls_back_to_the_same_mark() -> None:
    """A logo that 404s and a logo that was never compiled should look
    identical -- the subset can lag the API either way."""
    from ui.logos import initials_mark

    with_image = token_mark(coin("USDC", USDC), "ethereum", 24)
    assert isinstance(last_resort(with_image), type(initials_mark("USDC", 24)))


def test_a_mark_is_asked_for_twice_before_giving_up() -> None:
    """The marks are not warmed at publish, so each is fetched cold and an
    IPFS gateway that cannot find the block answers 504 after ~17s. Two of
    25 Gnosis marks did that on one measured pass and all of them served
    on the next, so one retry is the difference between a missing logo and
    a slow one."""
    from ui.logos import MARK_ATTEMPTS

    urls = attempts(token_mark(coin("USDC", USDC), "ethereum", 24))

    assert len(urls) == MARK_ATTEMPTS == 2


def test_the_retry_asks_for_a_different_url() -> None:
    """Flutter's `ImageCache` is keyed by URL and caches *failures*, so a
    retry on the same string is answered from that cache without a request
    -- which is why one failed fetch blanks a logo for the whole session.
    The query is ignored by the gateway; it exists to be a new cache key."""
    urls = attempts(token_mark(coin("USDC", USDC), "ethereum", 24))

    assert len(set(urls)) == len(urls)
    assert urls[0].endswith(".png")  # the file's own address, untouched
    assert urls[1].startswith(urls[0] + "?")


def test_a_token_with_no_art_at_all_never_asks_twice() -> None:
    """No image was compiled, so there is nothing to retry and the
    initials are not a fallback but the answer."""
    from ui.logos import initials_mark

    mark = token_mark(coin("NOPE", "0x" + "99" * 20), "ethereum", 24)

    assert attempts(mark) == []
    assert isinstance(mark.content, type(initials_mark("NOPE", 24)))


def test_the_fallback_colour_is_stable_per_symbol() -> None:
    """The same token should look the same everywhere it appears."""
    assert fallback_color("USDC") == fallback_color("USDC")
    assert fallback_color("USDC") != fallback_color("WBTC")


def test_fallback_colour_of_nothing_does_not_crash() -> None:
    assert fallback_color("")


# -- the stack -------------------------------------------------------------


def test_logos_overlap_rather_than_sit_side_by_side() -> None:
    size = 24
    stack = coin_stack([coin("A"), coin("B"), coin("C")], "ethereum", size)
    # Painted right to left -- see `coin_stack` -- so read the positions
    # back in the order they are laid out rather than the order they are
    # drawn in.
    lefts = sorted(c.left for c in stack.content.controls)
    step = lefts[1] - lefts[0]
    assert 0 < step < size, "each logo must overlap its neighbour"
    assert step == pytest.approx(size * (1 - OVERLAP))


def test_the_stack_is_wide_enough_for_every_logo() -> None:
    """A Stack sizes to its largest child, so the width must be set.

    Without it every pool would occupy one logo's width and overlap the
    next column.
    """
    size = 24
    stack = coin_stack([coin("A"), coin("B"), coin("C")], "ethereum", size)
    last = max(c.left for c in stack.content.controls)
    assert stack.width >= last + size


def test_a_long_pool_is_truncated_with_a_counter() -> None:
    coins = [coin(f"C{i}") for i in range(9)]
    stack = coin_stack(coins, "ethereum", 24, limit=4)
    labels = [
        c.content.value
        for c in stack.content.controls
        if isinstance(c.content, ft.Text)
    ]
    assert "+5" in labels


def test_an_empty_pool_still_produces_a_control() -> None:
    stack = coin_stack([], "ethereum", 24)
    assert stack.width == 0


def test_a_single_coin_needs_exactly_one_logo_of_width() -> None:
    stack = coin_stack([coin("A")], "ethereum", 24)
    assert stack.width == 24


# -- metapools -------------------------------------------------------------


def metapool() -> Pool:
    """A real shape: v2 returns [meta, baseLP, ...underlying]."""
    return Pool.from_v2(
        {
            "base_pool": "0xDcEF968d416a41Cdac0ED8702fAC8128A64241A2",
            "is_metapool": True,
            "chain_id": 1,
            "coins": [
                {"symbol": "msUSD", "address": "0x" + "aa" * 20},
                {"symbol": "crvFRAX", "address": "0x" + "bb" * 20},
                {"symbol": "FRAX", "address": "0x" + "cc" * 20},
                {"symbol": "USDC", "address": USDC},
            ],
        },
        "ethereum",
    )


def test_a_metapool_shows_its_underlying_not_its_lp_token() -> None:
    """Curve decomposes these, and the LP token is plumbing, not an asset."""
    assert metapool().coin_symbols == ["msUSD", "FRAX", "USDC"]


def test_the_stack_draws_the_decomposed_assets() -> None:
    stack = pool_stack(metapool(), size=24)
    assert len(stack.content.controls) == 3


def test_a_plain_pool_is_left_alone() -> None:
    plain = Pool.from_v2(
        {"coins": [{"symbol": s} for s in ("DAI", "USDC", "USDT")]}, "ethereum"
    )
    assert plain.coin_symbols == ["DAI", "USDC", "USDT"]
    assert len(pool_stack(plain).content.controls) == 3


def test_an_undecomposed_metapool_keeps_both_coins() -> None:
    """With only two coins there is nothing underlying to show instead."""
    pool = Pool.from_v2(
        {"base_pool": "0xabc", "coins": [{"symbol": "X"}, {"symbol": "3Crv"}]}
    )
    assert pool.coin_symbols == ["X", "3Crv"]


def test_contract_coins_stay_separate_from_displayed_ones() -> None:
    """`add_liquidity` takes a uint256[N] whose N is the *contract's*."""
    pool = metapool()
    pool.merge_detail({"n_coins": 2, "balances": [1.0, 2.0]})
    assert pool.n_coins == 2
    assert [c.symbol for c in pool.pool_coins] == ["msUSD", "crvFRAX"]
    assert pool.coin_symbols == ["msUSD", "FRAX", "USDC"]


def test_the_swap_and_withdraw_pickers_carry_marks() -> None:
    """A dropdown of bare symbols is harder to scan than one with logos."""
    from ui.actions import _coin_options

    options = _coin_options([coin("USDC", USDC), coin("WOOF")], "ethereum")
    assert [o.key for o in options] == ["0", "1"]
    assert [o.text for o in options] == ["USDC", "WOOF"]
    for option in options:
        assert isinstance(option.content, ft.Row)
        assert len(option.content.controls) == 2  # mark, then symbol


def test_balances_line_up_with_the_contract_coins() -> None:
    pool = metapool()
    pool.merge_detail({"n_coins": 2, "balances": [10.0, 20.0], "balances_usd": [10.0, 20.0]})
    assert [c.balance for c in pool.pool_coins] == [10.0, 20.0]


def test_marks_are_minified_with_mipmaps_from_the_full_image() -> None:
    """Both halves of this were wrong before, and they interact.

    `cache_width` decoded at 3x the display size, which throws away the
    resolution a mipmap chain is built from; and the quality was `high`,
    which is bicubic -- a *magnification* filter that reads a few source
    pixels per output pixel when reducing tenfold, so fine artwork comes
    out noisy. Compared side by side at 27px against the real assets,
    full-resolution decode with `medium` was the only combination that
    looked right.
    """
    mark = token_mark(coin("USDC", USDC), "ethereum", 26)
    image = mark.content
    assert isinstance(image, ft.Image)
    assert image.filter_quality == ft.FilterQuality.MEDIUM
    assert image.cache_width is None
    assert image.cache_height is None


# -- the app's own icons ---------------------------------------------------
#
# Unlike everything above, these are committed rather than compiled: a site
# needs a favicon whether or not whoever cloned it has initialised the
# curve-assets submodule, and `flet build` cannot read an SVG. They are
# regenerated by `tools/build_icons.py`.
#
# The names are Flet's, not ours. `flet publish` copies `src/assets` over
# its own web root after unpacking it, so a file only overrides the stock
# one if it lands at exactly the same path -- which is what these pin.

ICONS = Path(__file__).resolve().parent.parent / "src" / "assets"

EXPECTED_ICONS = {
    "favicon.png": 32,
    "icon.png": 1024,
    "icons/icon-192.png": 192,
    "icons/icon-512.png": 512,
    "icons/icon-maskable-192.png": 192,
    "icons/icon-maskable-512.png": 512,
    "icons/apple-touch-icon-192.png": 192,
    # What the page shows while the Python runtime starts, in place of
    # Flet's own logo. Same override as the favicon: same name, copied
    # over theirs.
    "icons/loading-animation.png": 512,
}


def png_size(path: Path) -> tuple[int, int]:
    """Width and height out of the IHDR chunk.

    Read by hand rather than with Pillow, which is only present as one of
    flet's test extras: whether these files exist at the names Flet expects
    is worth checking on a bare install too.
    """
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize("name,size", EXPECTED_ICONS.items())
def test_the_icon_exists_at_the_name_flet_expects(name: str, size: int) -> None:
    path = ICONS / name
    assert path.is_file(), f"{name} is missing -- run tools/build_icons.py"
    assert png_size(path) == (size, size)


@pytest.mark.parametrize(
    "name", [n for n in EXPECTED_ICONS if "maskable" in n or "apple" in n]
)
def test_a_cropped_icon_is_opaque_and_inside_its_safe_zone(name: str) -> None:
    """These get cut to a circle or a squircle, and iOS composites
    transparency onto black. So they carry their own background, and the
    mark stays inside the middle 80% that a maskable icon is promised."""
    image_module = pytest.importorskip("PIL.Image", reason="Pillow: pip install flet[test]")
    with image_module.open(ICONS / name) as image:
        image = image.convert("RGBA")
        assert image.getchannel("A").getextrema() == (255, 255), "not opaque"
        size = image.width
        assert image.getpixel((1, 1))[:3] == (255, 255, 255), "no backdrop"
        # The mark must not reach the edge: sample the middle of each side,
        # which is where a circular crop bites deepest.
        for point in ((size // 2, 1), (size // 2, size - 2), (1, size // 2)):
            assert image.getpixel(point)[:3] == (255, 255, 255)


def test_there_is_an_ico_for_the_path_browsers_probe() -> None:
    """Every browser asks for `/favicon.ico` before reading the link tag,
    and `flet publish` ships no such file."""
    path = ICONS / "favicon.ico"
    assert path.is_file(), "missing -- run tools/build_icons.py"
    assert path.read_bytes()[:4] == b"\x00\x00\x01\x00", "not an ICO"


def test_the_favicon_is_not_flets_default() -> None:
    """The whole point: `flet publish` ships its own favicon.png, and ours
    only wins by being copied over it."""
    stock = Path(ft.__file__).resolve().parent.parent / "flet_web" / "web" / "favicon.png"
    if not stock.is_file():  # pragma: no cover - depends on the install
        pytest.skip("flet_web is not installed")
    assert (ICONS / "favicon.png").read_bytes() != stock.read_bytes()


# -- how a mark is drawn ----------------------------------------------------


def test_a_mark_asks_the_browser_to_resize_nothing() -> None:
    """`cache_width` used to be set, and on WebKit it was the bug.

    Decoding to a size goes through the browser, and WebKit's resize path
    gets premultiplication wrong: it drew a pale rim around every mark
    that had one, on iOS only, because Chrome and Firefox decode by other
    routes. Reproduced in WebKitGTK at a device pixel ratio of 3 -- 21
    levels brighter than the header with the hint, exactly the background
    without it.

    It earned its place when the art was 200-280px. `build_assets.py`
    compiles to `MARK_PIXELS` now, so there is little left to resize and
    nothing to gain by asking.
    """
    marks = [
        logos.chain_mark("ethereum", 18),
        logos.token_mark(coin("USDC", USDC), "ethereum", 24).content,
    ]
    for mark in marks:
        assert isinstance(mark, ft.Image)
        assert mark.cache_width is None, "no mark may ask the browser to resize"
        assert mark.filter_quality is logos.SAMPLING


# -- what build_assets compiles them down to --------------------------------


def test_no_compiled_mark_is_larger_than_it_is_drawn(monkeypatch) -> None:
    """The point of shrinking them at build time.

    Upstream art is 200-280px and nothing here is drawn above 38, so the
    renderer was being handed a tenfold reduction and asked to make it
    look good. It cannot -- minification wants an average over every
    contributing pixel, which is a mipmap, and CanvasKit builds none. So
    the averaging happens once, offline, and what ships is close to the
    size it is used at.
    """
    from PIL import Image

    from ui.assets import MARK_PIXELS

    root = Path(__file__).resolve().parent.parent / "src/assets/curve"
    marks = [*(root / "chains").glob("*.png")]
    marks += sorted((root / "tokens/ethereum").glob("*.png"))[:120]
    assert marks, "assets are not compiled -- run tools/build_assets.py"

    for mark in marks:
        with Image.open(mark) as image:
            assert max(image.size) <= MARK_PIXELS, (
                f"{mark.name} is {image.size}, larger than MARK_PIXELS"
            )


def test_the_biggest_mark_still_has_pixels_for_a_dense_screen() -> None:
    """38px is the largest thing drawn -- the stack on a detail page --
    and the densest screens put four device pixels on each logical one.
    Nothing asks the browser to resize any more, so the top tier is the
    only thing standing between a mark and being drawn from too few
    pixels."""
    from ui.assets import MARK_PIXELS

    assert MARK_PIXELS >= 38 * 4


# -- and how far above it they are allowed to be ----------------------------
#
# The half that was missing, and the reason this bug outlived three fixes.
# Every check here used to be a *floor*: art must be at least large enough
# for the worst case. Nothing was a ceiling, so 160px art against a 27px
# mark satisfied the suite completely while looking exactly like the
# problem the suite was written to prevent.


def test_no_mark_is_drawn_from_far_more_art_than_it_needs() -> None:
    """The regression, stated as a rule.

    CanvasKit builds no mipmap chain, so `FilterQuality.MEDIUM` comes down
    to four bilinear taps however far the reduction is. Four taps over a
    six-pixel span is the mush that was reported -- and measured beside
    curve.finance, whose marks are `<img>` elements put through the
    browser's own downscaler.

    So the tier a mark is drawn from may sit above the size it is drawn
    at, but never more than twice above: four pixels averaged into one is
    a box filter, which is exactly what four bilinear taps compute and
    exactly what a mipmap level would have held. The tiers double so that
    the gap between any two of them is that bound.
    """
    from ui.assets import MARK_TIERS, mark_tier

    # Every size this app draws a mark at, against every ratio a screen
    # reports -- including the fractional ones desktop scaling produces,
    # which is where this was found.
    for size in (14, 18, 20, 22, 24, 26, 28, 34, 38):
        for ratio in (1.0, 1.140625, 1.25, 1.5, 2.0, 2.625, 3.0, 3.5, 4.0):
            drawn = size * ratio
            tier = mark_tier(drawn)
            assert tier >= drawn or tier == MARK_TIERS[-1], (
                f"{size}px at {ratio}x wants {drawn:.0f} and got {tier}"
            )
            assert tier <= drawn * 2, (
                f"{size}px at {ratio}x is {drawn:.0f} device pixels drawn from "
                f"{tier}px of art -- a {tier / drawn:.1f}x reduction for a "
                "filter that manages two"
            )


def test_a_tier_is_never_smaller_than_what_it_is_asked_for() -> None:
    """Rounding down would magnify, and magnification is the one direction
    no filter recovers from -- it invents nothing and smears what is
    there. Up to the ceiling, the answer is always at least the ask."""
    from ui.assets import MARK_TIERS, mark_tier

    for wanted in range(1, MARK_TIERS[-1] + 1):
        assert mark_tier(wanted) >= wanted
    # Past the top there is no more art, so that is what everything gets.
    assert mark_tier(MARK_TIERS[-1] + 1) == MARK_TIERS[-1]
    assert mark_tier(10_000) == MARK_TIERS[-1]


def test_every_mark_is_compiled_at_every_tier() -> None:
    """A tier that is asked for and was never written is a 404 and an
    unpainted mark -- and in the browser `_exists` cannot check, so it
    would ship silently."""
    from PIL import Image

    from ui.assets import MARK_TIERS

    root = Path(__file__).resolve().parent.parent / "src/assets/curve"
    stems = {p.name.split("@")[0] for p in (root / "chains").glob("*.png")}
    assert stems, "assets are not compiled -- run tools/build_assets.py"

    for stem in sorted(stems):
        for tier in MARK_TIERS:
            mark = root / "chains" / f"{stem}@{tier}.png"
            assert mark.is_file(), f"{mark.name} was never written"
            with Image.open(mark) as image:
                assert max(image.size) <= tier, (
                    f"{mark.name} is {image.size}, larger than the tier it names"
                )


def test_a_mark_its_parent_resizes_asks_for_the_top_tier() -> None:
    """The picker's leading icon is handed 14 and drawn at the field's
    height, so a tier chosen from what it was handed is art the field then
    magnifies. It was the one mark still looking like a low-resolution
    copy once the tokens beside it had come good.

    Not measured -- nothing here computes a layout size. It just stops
    asking low.
    """
    from ui.assets import MARK_PIXELS
    from ui.logos import chain_mark

    try:
        logos.set_pixel_ratio(1.0)
        stretched = chain_mark("ethereum", 14, sized_by_parent=True)
        assert f"@{MARK_PIXELS}.png" in str(stretched.src)
        # And the ordinary case is still chosen from what is drawn: a mark
        # nobody resizes must not pay for the top tier.
        laid_out = chain_mark("ethereum", 14)
        assert f"@{MARK_PIXELS}.png" not in str(laid_out.src)
    finally:
        logos.set_pixel_ratio(2.0)


def test_a_mark_asks_for_the_tier_that_covers_the_ratio_it_is_drawn_at() -> None:
    """The wiring, end to end: the pixel ratio the platform reported has
    to reach the filename, or the tiers are just extra files.

    A ratio of 1 is the only one where the logical size and the physical
    size cannot be told apart -- and every desktop window is a ratio of 1,
    which is how a bug that only exists above 1 hid for so long.
    """
    from ui import logos

    try:
        logos.set_pixel_ratio(1.0)
        assert "@20.png" in str(logos.chain_mark("ethereum", 18).src)
        logos.set_pixel_ratio(3.0)
        assert "@80.png" in str(logos.chain_mark("ethereum", 18).src)
        logos.set_pixel_ratio(4.0)
        assert "@160.png" in str(logos.chain_mark("ethereum", 38).src)
    finally:
        logos.set_pixel_ratio(2.0)


def test_a_compiled_mark_has_no_pale_rim() -> None:
    """The bug a phone found twice.

    A resampler averages each channel alone, so the colour of transparent
    pixels bleeds into the edge unless you premultiply first. That much
    was done -- and then the premultiplied data was rounded to uint8
    before resizing, which is worse: at alpha 16/255 a channel value of
    14.7 rounds to 15, and dividing that back out multiplies the error by
    1/alpha. On this app's own network mark blue came out at 494, clipped
    to 255, and the disc wore a white ring on iOS.

    So: every part-transparent pixel on the rim must still be the colour
    of the disc behind it, not a brighter version of it.

    Every tier, not just the largest: each is resampled from the original
    in its own right, and the smallest has the furthest to fall, so it is
    the one most likely to bleed.
    """
    import numpy as np
    from PIL import Image

    from ui.assets import MARK_TIERS

    root = Path(__file__).resolve().parent.parent / "src/assets/curve/chains"
    for tier in MARK_TIERS:
        mark = root / f"ethereum@{tier}.png"
        assert mark.is_file(), "assets are not compiled -- run tools/build_assets.py"

        pixels = np.asarray(Image.open(mark).convert("RGBA")).astype(int)
        solid = pixels[pixels[..., 3] == 255][..., :3]
        assert len(solid), f"the {tier}px mark has no opaque pixels at all"
        brightest_solid = solid.max()

        edge = pixels[(pixels[..., 3] > 0) & (pixels[..., 3] < 255)][..., :3]
        assert len(edge), f"the {tier}px mark has no antialiased edge to check"

        # Nothing on the rim may be brighter than the artwork it belongs
        # to. A halo shows up as exactly that: edge pixels lighter than
        # anything solid in the image.
        assert edge.max() <= brightest_solid + 1, (
            f"rim of the {tier}px mark reaches {edge.max()} where the artwork "
            f"peaks at {brightest_solid} -- that is a halo"
        )


def test_every_mark_fades_out_rather_than_stopping() -> None:
    """The outline has to live in the alpha channel.

    Upstream ships discs inscribed exactly in their square, so the edge
    was a *cut*: on one chain mark 140 fully opaque pixels sat on the
    bitmap border with no alpha between the colour and the end of the
    file. That reads as cropped because it is, and it leaves anything
    sampling across the boundary with nothing sensible to average.

    So the disc is cut into the alpha at build time, sampled 8x8 per
    pixel, and inset far enough that nothing touches the border.
    """
    import numpy as np
    from PIL import Image

    root = Path(__file__).resolve().parent.parent / "src/assets/curve"
    marks = [*(root / "chains").glob("*.png")][:20]
    marks += sorted((root / "tokens/ethereum").glob("*.png"))[:40]
    assert marks, "assets are not compiled -- run tools/build_assets.py"

    for mark in marks:
        alpha = np.asarray(Image.open(mark).convert("RGBA")).astype(int)[..., 3]
        border = np.concatenate(
            [alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]]
        )
        assert border.max() < 250, (
            f"{mark.name} is opaque against the bitmap border -- that edge "
            "is a crop, not an outline"
        )
        partial = ((alpha > 8) & (alpha < 248)).sum()
        assert partial > alpha.shape[0], (
            f"{mark.name} has {partial} part-transparent pixels, too few for "
            "an antialiased outline of that circumference"
        )


# -- the build refuses rather than degrades ---------------------------------
#
# The two tests above are what caught a silent half-build: run without
# Pillow, `build_assets.py` used to fall through to `shutil.copy2` and ship
# upstream's 200px art. The build printed its usual summary and exited 0, so
# nothing short of running the suite would tell you. These pin the fix --
# and the point is the *ordering*: it has to refuse before it deletes the
# assets it is about to fail to rebuild.


def test_missing_requirements_names_the_package_not_the_module() -> None:
    """"PIL" is not something you can install; "Pillow" is."""
    import importlib.util

    from tools import build_assets

    real = importlib.util.find_spec
    try:
        importlib.util.find_spec = lambda name: None  # type: ignore[assignment]
        assert build_assets.missing_requirements() == ["Pillow", "numpy"]
    finally:
        importlib.util.find_spec = real  # type: ignore[assignment]


def test_a_dev_environment_is_missing_nothing() -> None:
    """The dev group in pyproject.toml covers what the build actually needs."""
    from tools import build_assets

    assert build_assets.missing_requirements() == []


def test_the_build_refuses_before_it_deletes_anything(monkeypatch, capsys) -> None:
    """Exit 1 and say so, the way `build_icons.py` already does.

    `rmtree` is booby-trapped rather than merely observed: refusing *after*
    clearing `src/assets/curve` would leave no assets at all, which is a
    worse failure than the one being fixed.
    """
    from tools import build_assets

    def never(*_args, **_kwargs):
        raise AssertionError("the build started before checking its tools")

    monkeypatch.setattr(build_assets, "missing_requirements", lambda: ["Pillow"])
    monkeypatch.setattr(build_assets.shutil, "rmtree", never)

    assert build_assets.main() == 1
    message = capsys.readouterr().err
    assert "Pillow" in message
    assert "--group dev" in message, "say how to fix it, not just what broke"


# -- one file per chain instead of one per coin ----------------------------


def make_bundle(marks: dict) -> tuple[bytes, dict]:
    """A bundle the way `tools/build_assets.bundle_marks` writes one:
    the PNGs end to end, and where each one starts."""
    blob, index, at = bytearray(), {}, 0
    for address, data in marks.items():
        index[address] = (at, len(data))
        blob += data
        at += len(data)
    return bytes(blob), index


def remember_bundle_at(directory: str, tier: int, blob: bytes, index: dict) -> int:
    """`remember_bundle`, imported where it is used rather than at the top:
    this file loads `ui.assets` lazily inside each test."""
    from ui.assets import remember_bundle

    return remember_bundle(directory, tier, blob, index)


PNG_A = b"\x89PNG" + b"aaaa"
PNG_B = b"\x89PNG" + b"bbbbbb"


@pytest.fixture(autouse=True)
def _clean_bundles():
    from ui.assets import forget_bundles

    forget_bundles()
    yield
    forget_bundles()


def test_a_slice_is_the_original_file_byte_for_byte() -> None:
    """The bundle is a concatenation, not a container, which is what lets
    Pyodide use it with no decoder and no image library."""
    from ui.assets import bundled_mark, remember_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A, "0xbb": PNG_B})

    assert remember_bundle(token_bundle("xdai"), 80, blob, index) == 2
    assert bundled_mark("xdai", "0xAA", 80) == PNG_A
    assert bundled_mark("xdai", "0xbb", 80) == PNG_B


def test_a_token_not_in_the_bundle_falls_back_rather_than_blanking() -> None:
    from ui.assets import bundled_mark, remember_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A})
    remember_bundle(token_bundle("xdai"), 80, blob, index)

    assert bundled_mark("xdai", "0xffff", 80) is None
    assert bundled_mark("ethereum", "0xaa", 80) is None  # another chain


def test_a_truncated_slice_is_dropped_rather_than_shown() -> None:
    """A short slice is not a PNG, and `ft.Image` would draw nothing where
    asking for the file would have drawn a logo."""
    from ui.assets import bundled_mark, remember_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A})
    index["0xbb"] = (0, len(blob) + 999)  # runs off the end
    index["0xcc"] = (2, 4)  # lands mid-file, so no PNG magic

    assert remember_bundle(token_bundle("xdai"), 80, blob, index) == 1
    assert bundled_mark("xdai", "0xbb", 80) is None
    assert bundled_mark("xdai", "0xcc", 80) is None


async def test_a_missing_bundle_is_zero_rather_than_an_error() -> None:
    """Nothing here may break a page. A build with no bundles, a gateway
    that will not serve one, a truncated index -- all come back as zero
    and every mark asks for its own file, exactly as before bundles."""
    from ui.assets import bundled_mark, load_bundle, token_bundle

    async def dead(_url):
        raise OSError("404")

    assert await load_bundle(token_bundle("xdai"), 80, dead) == 0
    assert bundled_mark("xdai", "0xaa", 80) is None


async def test_a_bundle_is_fetched_once_per_chain_and_tier() -> None:
    """The point of it: one request for a chain, not one per coin."""
    from ui.assets import load_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A, "0xbb": PNG_B})
    asked = []

    async def fetch(url):
        asked.append(url)
        return json.dumps(index).encode() if url.endswith(".json") else blob

    assert await load_bundle(token_bundle("xdai"), 80, fetch) == 2
    assert await load_bundle(token_bundle("xdai"), 80, fetch) == 2  # cached, no new requests

    assert len(asked) == 2  # the blob and its index, once
    assert asked[0].endswith("marks@80.bin")
    assert asked[1].endswith("marks@80.json")


async def test_the_tier_asked_for_is_the_one_the_screen_needs() -> None:
    from ui.assets import load_bundle, mark_tier, token_bundle

    asked = []

    async def fetch(url):
        asked.append(url)
        raise OSError("stop here")

    await load_bundle(token_bundle("xdai"), 24 * 3, fetch)

    assert f"marks@{mark_tier(72)}.bin" in asked[0]


def test_a_bundled_mark_needs_no_retry_chain() -> None:
    """It is already in memory, so there is no request to fail and nothing
    to ask twice. The retry exists for files fetched from a gateway."""
    from ui.assets import remember_bundle, token_bundle

    blob, index = make_bundle({USDC.lower(): PNG_A})
    remember_bundle(token_bundle("ethereum"), 80, blob, index)

    mark = token_mark(coin("USDC", USDC), "ethereum", 24)
    urls = [src for src in attempts(mark) if isinstance(src, str)]

    assert urls == []  # nothing is fetched, so nothing can fail
    assert mark.content.src == PNG_A
    assert mark.content.error_content is not None  # still guards a bad slice


def test_only_the_tiers_screens_use_are_bundled() -> None:
    """A bundle is a second copy, so bundling all four tiers doubles 31.4
    MB of marks and hands back most of what dropping canvaskit/ and
    pyodide/ won. 40 and 80 are 10.9 MB of that and cover a 22-34px mark
    at 1x, 2x and 3x; 160 alone would be 19.1 MB for the rarest ratio."""
    from tools.build_assets import BUNDLE_TIERS
    from ui.assets import MARK_TIERS, mark_tier
    from ui.logos import MARK_SIZE

    assert set(BUNDLE_TIERS) < set(MARK_TIERS)
    assert 160 not in BUNDLE_TIERS
    # A 3x screen's *ideal* tier is past the top of this, which is what
    # `bundle_tier` exists to absorb -- see the mobile bug below. Bundling
    # 160 as well would serve it exactly and cost 19.1 MB.
    assert mark_tier(MARK_SIZE * 3) == 160


def test_the_bundle_asked_for_is_the_tier_the_screen_draws() -> None:
    """Not `MARK_PIXELS`, which is 160 and 19 MB of art no ordinary ratio
    draws -- asking for that was the first version of this and would have
    fetched the largest bundle on every visit."""
    from ui.assets import MARK_PIXELS, mark_tier
    from ui.logos import MARK_SIZE

    assert mark_tier(MARK_SIZE * 2) != MARK_PIXELS


# -- the big chains arrive in two halves -----------------------------------


def test_the_second_half_adds_to_the_first_rather_than_replacing_it() -> None:
    """A split chain arrives twice, and the tail must not evict the head:
    the head is what the visible rows were drawn from."""
    from ui.assets import bundled_mark, remember_bundle, token_bundle

    hot_blob, hot_index = make_bundle({"0xaa": PNG_A})
    rest_blob, rest_index = make_bundle({"0xbb": PNG_B})

    assert remember_bundle(token_bundle("ethereum"), 80, hot_blob, hot_index) == 1
    assert remember_bundle(token_bundle("ethereum"), 80, rest_blob, rest_index) == 2

    assert bundled_mark("ethereum", "0xaa", 80) == PNG_A
    assert bundled_mark("ethereum", "0xbb", 80) == PNG_B


async def test_the_tail_is_asked_for_under_its_own_name() -> None:
    from ui.assets import REST_INFIX, load_bundle, token_bundle

    asked = []

    async def fetch(url):
        asked.append(url)
        raise OSError("404")

    await load_bundle(token_bundle("ethereum"), 80, fetch, rest=True)

    assert asked[0].endswith(f"marks@80{REST_INFIX}.bin")


async def test_a_chain_that_was_never_split_just_returns_zero() -> None:
    """Most chains have no tail, so asking for one is a 404 and that has
    to be ordinary rather than an error."""
    from ui.assets import load_bundle, token_bundle

    async def missing(_url):
        raise OSError("404")

    assert await load_bundle(token_bundle("xdai"), 80, missing, rest=True) == 0


async def test_the_tail_is_fetched_even_though_the_head_is_cached() -> None:
    """The head-is-cached short circuit must not skip the tail, or a split
    chain would only ever show its hottest 150 marks."""
    from ui.assets import load_bundle, token_bundle

    hot_blob, hot_index = make_bundle({"0xaa": PNG_A})
    rest_blob, rest_index = make_bundle({"0xbb": PNG_B})
    asked = []

    async def fetch(url):
        asked.append(url)
        blob, index = (rest_blob, rest_index) if "-rest" in url else (hot_blob, hot_index)
        return json.dumps(index).encode() if url.endswith(".json") else blob

    assert await load_bundle(token_bundle("ethereum"), 80, fetch) == 1
    assert await load_bundle(token_bundle("ethereum"), 80, fetch, rest=True) == 2
    assert sum("-rest" in url for url in asked) == 2


def test_the_hot_half_is_ranked_and_capped() -> None:
    """Ranked by how many pools hold a token, over pools ordered by volume,
    so "hot" is what a visitor is most likely to see. 150 buys 93% of the
    first page's marks for a quarter of Ethereum's bytes."""
    from pathlib import Path

    from tools.build_assets import HOT_TOKENS, split_marks

    marks = [Path(f"0x{i:02x}@80.png") for i in range(200)]
    order = [f"0x{i:02x}" for i in reversed(range(200))]

    hot, rest = split_marks(marks, order)

    assert len(hot) == HOT_TOKENS == 150
    assert hot[0].name.startswith("0xc7")  # the top of the ranking
    assert len(rest) == 50
    assert not set(hot) & set(rest)


def test_a_token_no_pool_holds_goes_in_the_tail() -> None:
    """An unranked token is one no pool on this chain holds. Guessing
    about it is not better than putting it last."""
    from pathlib import Path

    from tools.build_assets import split_marks

    marks = [Path("0xaa@80.png"), Path("0xzz@80.png")]

    hot, rest = split_marks(marks, ["0xaa"])

    assert [m.name for m in hot] == ["0xaa@80.png"]
    assert [m.name for m in rest] == ["0xzz@80.png"]


def test_no_ranking_means_one_bundle_rather_than_a_bad_split(tmp_path) -> None:
    """A build must not need the API to be up. With no ranking the chain
    is bundled whole, which is what every small chain gets anyway."""
    from tools.build_assets import bundle_marks

    for i in range(3):
        (tmp_path / f"0x{i:02x}@80.png").write_bytes(b"\x89PNG" + b"x" * 100)

    bundle_marks(tmp_path, [])

    assert (tmp_path / "marks@80.bin").is_file()
    assert not (tmp_path / "marks@80-rest.bin").exists()


async def test_a_bundle_in_hand_is_never_fetched_twice() -> None:
    """`load_pools` runs once at startup and again when a deep link is
    applied, and the tail has no entry of its own in the store -- so
    asking the store "is it cached" refetched 2.2 MB of Ethereum on every
    visit."""
    from ui.assets import load_bundle, token_bundle

    hot_blob, hot_index = make_bundle({"0xaa": PNG_A})
    asked = []

    async def fetch(url):
        asked.append(url)
        if "-rest" in url:
            raise OSError("404")
        return json.dumps(hot_index).encode() if url.endswith(".json") else hot_blob

    for _ in range(3):
        await load_bundle(token_bundle("ethereum"), 80, fetch)
        await load_bundle(token_bundle("ethereum"), 80, fetch, rest=True)

    assert sum("-rest" not in url for url in asked) == 2  # the blob and its index


async def test_a_failed_bundle_is_asked_for_once_more_and_then_left() -> None:
    """The ask is what warms the block, and one bundle failing takes every
    mark on the page with it.

    Measured on the published site: `curve/tokens/ethereum/marks@80.bin`
    -- the tier every phone asks for, where a 1x desktop asks for 40 --
    answered 504 after 17.7s and served in 1.07s the next time. So a
    second ask, and only a second: a chain with no tail 404s here and
    must not re-ask on every reload.
    """
    from ui.assets import BUNDLE_ATTEMPTS, load_bundle, token_bundle

    asked = []

    async def cold(url):
        asked.append(url)
        raise OSError("504")

    for _ in range(4):
        await load_bundle(token_bundle("ethereum"), 80, cold)

    assert BUNDLE_ATTEMPTS == 2
    assert sum(url.endswith(".bin") for url in asked) == BUNDLE_ATTEMPTS


async def test_the_second_ask_is_the_one_that_lands() -> None:
    """Which is the whole point of making it -- the cold-block pattern is
    a 504 and then the file."""
    from ui.assets import bundled_mark, load_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A})
    tries = []

    async def warming(url):
        tries.append(url)
        if len(tries) == 1:
            # The first ask is what warms the block, and it is the `.bin`
            # that goes first -- so the index is never even reached.
            raise OSError("504")
        return json.dumps(index).encode() if url.endswith(".json") else blob

    assert await load_bundle(token_bundle("xdai"), 80, warming) == 0
    assert await load_bundle(token_bundle("xdai"), 80, warming) == 1
    assert bundled_mark("xdai", "0xaa", 80) == PNG_A


async def test_two_page_loads_do_not_pull_the_same_bundle_at_once() -> None:
    """The note is taken when the fetch *starts*, not when it lands: the
    deep link runs `load_pools` a second time while the first is still in
    flight, and both pulling 2.2 MB is the bug the note exists to stop."""
    import asyncio

    from ui.assets import load_bundle, token_bundle

    blob, index = make_bundle({"0xaa": PNG_A})
    asked = []

    async def slow(url):
        asked.append(url)
        await asyncio.sleep(0)
        return json.dumps(index).encode() if url.endswith(".json") else blob

    await asyncio.gather(
        load_bundle(token_bundle("ethereum"), 80, slow),
        load_bundle(token_bundle("ethereum"), 80, slow),
    )

    assert sum(url.endswith(".bin") for url in asked) == 1


# -- the tier a phone actually asks for ------------------------------------


def test_a_screen_past_the_largest_bundle_still_gets_one() -> None:
    """The mobile bug. A mark is drawn at 27 logical pixels in the list,
    so a 3x phone's ideal tier is 160 -- which is not bundled. Asking for
    it 404s and drops every mark on the page back to its own file, which
    is how a USDC logo went missing on Gnosis on a phone while the desktop
    beside it was fine."""
    from ui.assets import BUNDLED_TIERS, bundle_tier, mark_tier
    from ui.logos import MARK_SIZE

    assert mark_tier(MARK_SIZE * 3) not in BUNDLED_TIERS  # the trap
    assert bundle_tier(MARK_SIZE * 3) in BUNDLED_TIERS  # and the way out
    assert bundle_tier(MARK_SIZE * 4) in BUNDLED_TIERS


@pytest.mark.parametrize("ratio", [1, 2, 2.625, 3, 4])
def test_every_ratio_a_real_screen_reports_gets_a_bundle(ratio) -> None:
    from ui.assets import BUNDLED_TIERS, bundle_tier
    from ui.logos import MARK_SIZE

    assert bundle_tier(MARK_SIZE * ratio) in BUNDLED_TIERS


def test_the_clamp_never_asks_for_art_smaller_than_it_must() -> None:
    """It rounds up while there is a tier to round up to, so the common
    ratios are unchanged -- only the ones past the top are clamped."""
    from ui.assets import bundle_tier

    assert bundle_tier(20) == 40
    assert bundle_tier(41) == 80
    assert bundle_tier(80) == 80
    assert bundle_tier(81) == 80  # past the top: the largest there is
    assert bundle_tier(400) == 80


def test_the_loader_and_the_lookup_agree_on_the_tier() -> None:
    """They must, or the app fetches one bundle and reads another. That is
    silent: every mark misses and the bundles simply stop being used."""
    from ui.assets import bundle_tier, bundled_mark, remember_bundle, token_bundle
    from ui.logos import MARK_SIZE

    drawn = MARK_SIZE * 3
    blob, index = make_bundle({"0xaa": PNG_A})
    remember_bundle(token_bundle("xdai"), bundle_tier(drawn), blob, index)

    assert bundled_mark("xdai", "0xaa", drawn) == PNG_A


def test_the_build_and_the_app_share_one_list_of_tiers() -> None:
    """Two copies drifting apart would write bundles nothing loads."""
    from tools.build_assets import BUNDLE_TIERS
    from ui.assets import BUNDLED_TIERS

    assert BUNDLE_TIERS is BUNDLED_TIERS


# -- the network marks, 160 files down to two ------------------------------


def test_the_network_marks_share_one_bundle() -> None:
    """34 chains, and the picker draws all of them the moment it opens.
    One file for every network rather than one each -- 160 files and 444
    KB was the last multi-file family in the build."""
    from ui.assets import CHAINS, bundled_chain, remember_bundle

    blob, index = make_bundle({"ethereum": PNG_A, "xdai": PNG_B})
    remember_bundle(CHAINS, 80, blob, index)

    assert bundled_chain("ethereum", 80) == PNG_A
    assert bundled_chain("xdai", 80) == PNG_B
    assert bundled_chain("nowhere", 80) is None


def test_the_two_families_do_not_collide() -> None:
    """`chains` and `tokens/<chain>` are different bundles, and a chain
    named the same as nothing in particular must not read one for the
    other -- they are keyed by the directory the marks live in."""
    from ui.assets import (
        CHAINS,
        bundled_chain,
        bundled_mark,
        remember_bundle,
        token_bundle,
    )

    chain_blob, chain_index = make_bundle({"ethereum": PNG_A})
    token_blob, token_index = make_bundle({"ethereum": PNG_B})
    remember_bundle(CHAINS, 80, chain_blob, chain_index)
    remember_bundle(token_bundle("ethereum"), 80, token_blob, token_index)

    assert bundled_chain("ethereum", 80) == PNG_A
    assert bundled_mark("ethereum", "ethereum", 80) == PNG_B


def test_a_network_mark_from_the_bundle_needs_no_retry() -> None:
    """Same reasoning as a coin's: it is in memory, so there is no request
    to fail and nothing to ask twice."""
    from ui.assets import CHAINS, bundle_tier, remember_bundle
    from ui.logos import chain_mark, pixel_ratio

    blob, index = make_bundle({"ethereum": PNG_A})
    remember_bundle(CHAINS, bundle_tier(18 * pixel_ratio()), blob, index)

    mark = chain_mark("ethereum", 18)

    assert mark.src == PNG_A
    assert isinstance(mark.src, bytes)


def test_a_bundle_is_read_at_the_tier_it_was_fetched_at() -> None:
    """One directory is fetched at one tier and read at several, and this
    is the gap that let a network logo go missing after the marks were
    bundled.

    `_load_marks` picks the tier from `MARK_SIZE` -- 27, a coin in the
    list -- and the picker reads the same store for a mark drawn at 18.
    At a device pixel ratio of 2 that is tier 80 written and tier 40
    asked for: an exact lookup misses, every network falls back to its
    own unwarmed file, and one cold block is a blank circle in the open
    menu. A ratio of 1 or 3 happens to agree, which is what made it look
    like weather.
    """
    from ui.assets import CHAINS, bundle_tier, bundled_chain
    from ui.logos import MARK_SIZE, chain_mark, set_pixel_ratio

    set_pixel_ratio(2.0)
    try:
        blob, index = make_bundle({"ethereum": PNG_A})
        fetched = bundle_tier(MARK_SIZE * 2.0)
        remember_bundle_at(CHAINS, fetched, blob, index)

        assert bundle_tier(18 * 2.0) != fetched, "the sizes must disagree"
        assert bundled_chain("ethereum", 18 * 2.0) == PNG_A
        assert chain_mark("ethereum", 18).src == PNG_A
    finally:
        set_pixel_ratio(2.0)


def test_a_mark_wanted_larger_than_anything_fetched_uses_the_largest() -> None:
    """The picker's own field asks for the top tier, because a decoration
    box stretches it. Art in hand beats a request every time -- see
    `chain_mark`'s `sized_by_parent`."""
    from ui.assets import CHAINS, bundled_chain

    blob, index = make_bundle({"ethereum": PNG_A})
    remember_bundle_at(CHAINS, 40, blob, index)

    assert bundled_chain("ethereum", 400) == PNG_A


def test_a_tier_that_covers_it_is_preferred_to_a_larger_one() -> None:
    """Rounding up is still rounding up: the smallest fetched tier that
    covers the mark, so the renderer is left the reduction it does well."""
    from ui.assets import CHAINS, bundled_chain

    small, small_index = make_bundle({"ethereum": PNG_A})
    large, large_index = make_bundle({"ethereum": PNG_B})
    remember_bundle_at(CHAINS, 40, small, small_index)
    remember_bundle_at(CHAINS, 80, large, large_index)

    assert bundled_chain("ethereum", 30) == PNG_A
    assert bundled_chain("ethereum", 70) == PNG_B
