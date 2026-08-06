"""How much of the UI fits, as a pure function of the window width.

Every responsive decision in the app comes from `layout_for(width)`, so the
breakpoints live in one place and can be checked exhaustively without a
window -- which matters here, because Flet's integration tests run at a
fixed size and cannot resize the page in device mode.

The pool list has five columns and they do not all survive a phone. The
order they are dropped in is by how much they help you *choose* a pool:
base APY goes first (it is the smallest number and the least decisive),
then the table becomes cards, because five columns squeezed into 400px is
worse than two lines of text.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Below this the pool table becomes a stack of cards.
CARD_BREAKPOINT = 760.0
#: Below this the table drops its least decisive column.
COMPACT_BREAKPOINT = 1000.0
#: Below this the pool page stacks the chart above the action panel instead
#: of putting them side by side. A 360px action panel next to a chart needs
#: roughly this much before the chart stops being a sliver.
STACK_BREAKPOINT = 900.0


@dataclass(slots=True, frozen=True)
class Layout:
    """What the current window width affords."""

    #: "wide" | "compact" | "cards" -- for tests and debugging.
    name: str
    #: Metric columns the pool table shows, in order. Empty when `cards`.
    columns: tuple[str, ...]
    #: Render pool rows as two-line cards rather than table rows.
    cards: bool
    #: Stack the pool page vertically instead of side by side.
    stacked: bool

    @property
    def shows_column_headers(self) -> bool:
        """Cards have no columns to head, so they get a sort dropdown."""
        return not self.cards


ALL_COLUMNS = ("base", "incentives", "volume", "tvl")
COMPACT_COLUMNS = ("incentives", "volume", "tvl")


def layout_for(width: float) -> Layout:
    """The layout for a window of this width."""
    stacked = width < STACK_BREAKPOINT
    if width < CARD_BREAKPOINT:
        return Layout("cards", (), cards=True, stacked=stacked)
    if width < COMPACT_BREAKPOINT:
        return Layout("compact", COMPACT_COLUMNS, cards=False, stacked=stacked)
    return Layout("wide", ALL_COLUMNS, cards=False, stacked=stacked)
