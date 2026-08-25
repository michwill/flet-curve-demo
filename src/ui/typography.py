"""One scale for every piece of text in the app."""

from __future__ import annotations

#: Anything you actually read: table cells, amounts, dropdown entries.
BODY = 15

#: The second line of a pair -- assets under a pool name, a balance under an
#: amount, a reward token under the CRV range.
SMALL = 13

#: Column headings, section labels, addresses. Present but not competing.
LABEL = 12

#: The smallest thing on screen: chart axis ticks and the crosshair.
TINY = 11

#: A pool's name in the list, where it is the row's subject.
ROW_TITLE = 16

#: A headline figure: the TVL and volume beside a pool's name.
METRIC = 20

#: The pool page's title, and the wordmark in the header, which is sized to
#: match it -- the app's name and the page's subject carry the same weight.
TITLE = 24

#: The same title where the page is a phone.
TITLE_NARROW = 20


#: Roughly how wide a glyph is, as a fraction of the font size, for the font
#: Flutter falls back to.  Grouped rather than tabulated per character: the
#: point is to size a box that holds the text, not to typeset it, and the
#: groups are what separate `Depth: WBTC / crvUSD` from `LP token`.
_NARROW = frozenset("iljtfI.,:;'`|!()[]")
_SPACE = frozenset(" /\\")
_WIDE = frozenset("WMmw@%")

#: What each group costs, in ems.  Measured off Roboto's advances and then
#: rounded up, because a box a little too wide reads as a box and a box a
#: little too narrow reads as a bug.
_NARROW_EM = 0.34
_SPACE_EM = 0.32
_WIDE_EM = 0.92
_UPPER_EM = 0.68
_LOWER_EM = 0.56


def text_width(text: str, size: float) -> float:
    """About how wide `text` will draw, in logical pixels.

    An estimate, and deliberately a generous one.  Flet has no way to measure
    a string before it is drawn, so a control sized to its contents has to
    guess -- and the guess should err wide, where the cost is a little empty
    space rather than a clipped word.
    """
    total = 0.0
    for glyph in text:
        if glyph in _NARROW:
            total += _NARROW_EM
        elif glyph in _SPACE:
            total += _SPACE_EM
        elif glyph in _WIDE:
            total += _WIDE_EM
        elif glyph.isupper() or glyph.isdigit():
            total += _UPPER_EM
        else:
            total += _LOWER_EM
    return total * size
