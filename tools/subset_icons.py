#!/usr/bin/env python3
"""Cut the icon font down to the icons this app actually draws.

    python tools/subset_icons.py            # rewrite dist/ in place
    python tools/subset_icons.py --check     # say what would be kept

**1.26 MB to draw ten glyphs.** `MaterialIcons-Regular.otf` carries 8,624
of them -- every Material icon in four styles -- and it is the largest
single thing a visitor downloads from the pin, larger than the whole rest
of the page put together. It is also the one file the gateway does not
compress, because it types `.otf` as an OpenDocument formula template and
that is not on its compressible list. Subsetting beats fixing that twice
over: gzip would take it to 459 KB, and keeping only what is drawn takes
it to a few.

Two sources for what to keep, and neither is a list anybody has to
maintain by hand:

  * **what the app asks for**, read out of `src/` -- `ft.Icons.SEARCH` and
    friends -- and resolved through the font's own glyph names. Adding an
    icon to the app adds it here, with nothing to remember.

    Not through `ft.Icons.SEARCH.value`, which looks like a codepoint and
    is not one: it is 72141, and the glyph is at 0xE567. The number is
    Flet's own handle for the icon and means nothing to this font. The
    glyph names do line up -- `SEARCH` is `search_baseline`, and a name
    that already carries a style keeps it -- so that is the join, and a
    name that does not resolve stops the build;
  * **what Flutter's own widgets draw**, which is the half this cannot
    read: the picker's chevron and the expansion tile's arrows come from
    Dart, not from Python. Those are named below, and a name that stops
    resolving fails the build rather than going quietly missing.

Note this font is Flet's own rebuild rather than Google's: the codepoints
are reassigned (classic `0xE5C5` is `smart_toy` here, not
`arrow_drop_down`), so nothing about the standard Material tables applies
and the glyph names in this file are read back from the font itself.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

#: Where `flet publish` copies the font from, and where it lands.
FONT_RELATIVE = "assets/fonts/MaterialIcons-Regular.otf"

#: How the app names an icon. Anything matching this is kept.
ICON_USE = re.compile(r"\bft\.Icons\.([A-Z0-9_]+)\b")

#: Glyphs Flutter draws by itself, which no scan of `src/` can find.
#:
#: Deliberately generous. A missing one is a tofu box in some state
#: nobody looked at, and each of these costs about 150 bytes against the
#: 1.26 MB being removed -- so the cheap mistake is keeping too many.
#: Named rather than numbered because the codepoints here are Flet's own
#: and would mean nothing to a reader.
WIDGET_GLYPHS = (
    # The chain picker, and every other dropdown.
    "arrow_drop_down_baseline",
    "arrow_drop_up_baseline",
    # The pool-parameters tile, and any other ExpansionTile.
    "expand_more_baseline",
    "expand_less_baseline",
    "keyboard_arrow_down_baseline",
    "keyboard_arrow_up_baseline",
    "keyboard_arrow_left_baseline",
    "keyboard_arrow_right_baseline",
    # Dialogs, snackbars, text fields that can be cleared.
    "close_baseline",
    "clear_baseline",
    "cancel_baseline",
    # Anything Material puts in front of a decision.
    "check_baseline",
    "check_box_baseline",
    "check_box_outline_blank_baseline",
    "radio_button_checked_baseline",
    "radio_button_unchecked_baseline",
    # What it reaches for when something has gone wrong.
    "error_baseline",
    "error_outline_baseline",
    "warning_baseline",
    "info_baseline",
    # Overflow menus and the back arrow a navigator supplies.
    "more_vert_baseline",
    "more_horiz_baseline",
    "arrow_back_baseline",
    "arrow_forward_baseline",
    "arrow_back_ios_baseline",
    "arrow_forward_ios_baseline",
)


#: The four faces Material draws every icon in. A Flet name that ends in
#: one of these already says which it wants; anything else means the
#: default, which the font calls `_baseline`.
STYLES = ("outlined", "rounded", "sharp")


def glyph_name(icon: str) -> str:
    """`COPY_ALL_OUTLINED` -> `copy_all_outlined`, `SEARCH` -> `search_baseline`."""
    lowered = icon.lower()
    if lowered.endswith(STYLES):
        return lowered
    return f"{lowered}_baseline"


def app_icon_names(src: Path = SRC) -> list[str]:
    """Every `ft.Icons.NAME` written in the source, in sorted order."""
    names: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        names.update(ICON_USE.findall(path.read_text(encoding="utf-8")))
    return sorted(names)


def app_icons(font, src: Path = SRC) -> dict[str, int]:
    """Every icon the app draws, as `ft.Icons` name -> codepoint.

    Raises if a name does not resolve to a glyph. That is either a typo,
    which would have drawn nothing at runtime anyway, or a naming rule
    that has moved -- and both are worth stopping a build over, because
    the alternative is shipping a font without it.
    """
    import flet as ft

    reverse = _by_glyph(font)
    found: dict[str, int] = {}
    for name in app_icon_names(src):
        if getattr(ft.Icons, name, None) is None:
            raise SystemExit(f"src/ names ft.Icons.{name}, which does not exist")
        glyph = glyph_name(name)
        if glyph not in reverse:
            raise SystemExit(
                f"ft.Icons.{name} should be the glyph {glyph!r}, which this "
                "font does not have -- see glyph_name()"
            )
        found[name] = reverse[glyph]
    return found


def _by_glyph(font) -> dict[str, int]:
    """Glyph name -> the codepoint that reaches it."""
    reverse: dict[str, int] = {}
    for codepoint, glyph in font.getBestCmap().items():
        reverse.setdefault(glyph, codepoint)
    return reverse


def widget_codepoints(font) -> dict[str, int]:
    """The named glyphs above, as name -> codepoint, read from the font."""
    reverse = _by_glyph(font)
    missing = [name for name in WIDGET_GLYPHS if name not in reverse]
    if missing:
        raise SystemExit(
            "these glyphs are not in the font any more, so the names in "
            f"WIDGET_GLYPHS are stale: {', '.join(missing)}"
        )
    return {name: reverse[name] for name in WIDGET_GLYPHS}


def keep(font) -> set[int]:
    """Every codepoint the subset must carry."""
    return set(app_icons(font).values()) | set(widget_codepoints(font).values())


def subset(source: Path, destination: Path) -> tuple[int, int, int]:
    """Write `source` to `destination` with only the kept codepoints.

    Returns (glyphs kept, bytes before, bytes after).
    """
    from fontTools import subset as ft_subset
    from fontTools.ttLib import TTFont

    # Read before writing: `destination` is usually `source`, and a size
    # taken afterwards would report the new file as the old one.
    before = source.stat().st_size
    font = TTFont(source)
    wanted = keep(font)

    options = ft_subset.Options()
    # Kept: the font has to stay a font Skia will parse. Dropped: the
    # tables that describe 8,000 glyphs nobody is drawing.
    options.desubroutinize = True
    options.drop_tables += ["FFTM"]
    options.notdef_outline = True

    subsetter = ft_subset.Subsetter(options=options)
    subsetter.populate(unicodes=wanted)
    subsetter.subset(font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    font.save(destination)

    after = TTFont(destination)
    return len(after.getGlyphOrder()), before, destination.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist", type=Path, default=ROOT / "dist", help="the build to rewrite"
    )
    parser.add_argument(
        "--check", action="store_true", help="report what would be kept, change nothing"
    )
    options = parser.parse_args()

    font = options.dist / FONT_RELATIVE
    if not font.is_file():
        raise SystemExit(f"{font} is not there -- run `flet publish` first")

    from fontTools.ttLib import TTFont

    icons = app_icons(TTFont(font))
    print(f"{len(icons)} icons named in src/:")
    for name, codepoint in icons.items():
        print(f"    ft.Icons.{name:<26} {glyph_name(name):<34} U+{codepoint:04X}")
    print(f"{len(WIDGET_GLYPHS)} more for Flutter's own widgets")

    if options.check:
        return 0

    glyphs, before, after = subset(font, font)
    print(
        f"{font.relative_to(options.dist)}: {before / 1024:,.0f} KB -> "
        f"{after / 1024:,.0f} KB, {glyphs} glyphs "
        f"({100 - after * 100 / before:.1f}% smaller)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
