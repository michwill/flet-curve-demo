"""One scale for every piece of text in the app.

The sizes used to be literals scattered across five modules, which made
"make the text a bit larger" a hunt rather than an edit. They are named by
role here, and the roles are anchored to the one size the app does not
choose: `BODY` matches the text inside a Material text field, so a table
cell does not read as a footnote beside the input next to it.

Each step is roughly a point apart. Going bigger everywhere means the
narrow layouts have less room, which is why the pool list drops a column
below 1000px -- see `responsive`.
"""

from __future__ import annotations

#: Anything you actually read: table cells, amounts, dropdown entries.
#: Matches a text field's own text.
BODY = 15

#: The second line of a pair -- assets under a pool name, a balance under
#: an amount, a reward token under the CRV range.
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
