"""Resolving compiled curve-assets paths, and the marks built from them.

These run against the real compiled output, so they also serve as a check
that `tools/build_assets.py` produced something usable. Where a test would
depend on a specific token existing upstream, it asserts the *shape* of the
answer instead.
"""

from __future__ import annotations

import struct
from pathlib import Path

import flet as ft
import pytest

from curve.models import Coin, Pool
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


def test_a_missing_image_falls_back_to_the_same_mark() -> None:
    """A logo that 404s and a logo that was never compiled should look
    identical -- the subset can lag the API either way."""
    from ui.logos import initials_mark

    with_image = token_mark(coin("USDC", USDC), "ethereum", 24)
    assert isinstance(with_image.content.error_content, type(initials_mark("USDC", 24)))


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
