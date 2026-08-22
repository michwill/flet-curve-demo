"""The Swap page: any coin to any coin, through the electric router.

Curve's own swap widget is the shape people expect and this follows it -- sell
box, buy box, the figures underneath, an approve step and a swap step -- with
one addition the router earns: on a wide screen the route is drawn beside it,
because this one *splits*, and a picture of a single path would be a picture
of the special case.

What this file owns is the widget.  Deciding when a quote happens belongs to
`router.host`, which has no Flet in it, and drawing the route belongs to
`ui.routegraph`, which has no Flet in its arithmetic.
"""

from __future__ import annotations

from itertools import pairwise

import flet as ft
import flet.canvas as cv

from curve.format import token_amount
from curve.models import Coin
from router.universe import CoinEntry, matching_coins
from wallet.erc20 import format_units, parse_units

from . import AnyEvent, buttons, routegraph, safe_update, theme
from .logos import token_mark
from .responsive import Layout
from .status import StatusPanel
from .typography import BODY, LABEL, SMALL, TITLE

#: How wide the widget is allowed to get.  Curve's own is about this, and a
#: swap box that spans a desktop window reads as a form rather than a control.
WIDGET_WIDTH = 480.0

#: What the diagram is given when it is stacked under the widget rather than
#: set beside it.  Beside the widget it takes whatever is left.
DIAGRAM_MIN_WIDTH = 420.0

#: How tall the paper is.  A number rather than the widget's own height:
#: Flet has no `IntrinsicHeight`, and a `Row` that stretches its children needs
#: a bounded height, which inside the page's scroller there is none of --
#: asking for it drew nothing at all.  Set to about what the widget stands at
#: with both buttons showing, and left there: the widget grows and shrinks as
#: an approval step or a status line comes and goes, and a picture that
#: followed it would be the most restless thing on the page.
DIAGRAM_HEIGHT = 440.0

#: How tall the route is when it is stacked under the widget instead of set
#: beside it, where there is less width for the same picture.
DIAGRAM_STACKED_HEIGHT = 300.0

#: What the frame says before there is a route in it -- so that the frame can
#: be there from the start rather than appearing under someone's hands.
EMPTY_ROUTE = "The route appears here"

#: Air either side of a column's label.  Two labels closer than this are two
#: words read as one, so the later of them is left out -- an eighteen-leg
#: route, which this router really does produce, puts a column every twenty
#: pixels.  The bands still show the shape; the names that survive are the
#: ones with room to be read.
LABEL_AIR = 4.0

#: How wide a column's label is allowed to be, and never less than.  A label
#: claims only the room its own text needs: measuring them all at the widest
#: made a five-letter name push its neighbour out for nothing.
LABEL_BOX = 96.0
LABEL_MIN = 34.0

#: How far under its column a label sits.
LABEL_DROP = 5.0

#: The size a coin's mark is drawn at in the picker and the boxes.
MARK = 24

#: Above this a price impact is worth a warning rather than a number.  The
#: same threshold the in-pool swap uses, so the two tabs agree about what
#: "high" means.
IMPACT_HIGH_BP = 100.0

#: What the picker's popped-open list is sized to.
PICKER_WIDTH = 340
PICKER_HEIGHT = 420

#: How wide the closed picker sits in a box.  A `SearchBar` has no width of
#: its own, so beside an expanding amount field it took the whole row and the
#: amount disappeared -- which is not a layout to leave to whoever wins.
PICKER_BAR_WIDTH = 148


def as_coin(entry: CoinEntry) -> Coin:
    """A `CoinEntry` in the shape `token_mark` and the formatters want."""
    return Coin(address=entry.address, symbol=entry.symbol,
                decimals=entry.decimals)


class CoinPicker(ft.SearchBar):
    """A searchable coin selector, ordered by how busy the coin is.

    The network picker's control, for the network picker's reasons: a
    `SearchBar` is two things, so the bar names the coin and the box a tap
    opens is empty -- an editable `Dropdown` would put typed letters into the
    middle of the symbol already in it.
    """

    def __init__(self, page: ft.Page, chain: str, *, on_pick, label: str):
        self._page = page
        self._chain = chain
        self._entries: list[CoinEntry] = []
        self._picked: CoinEntry | None = None
        self._on_pick = on_pick
        super().__init__(
            value="",
            bar_hint_text=label,
            bar_text_style=ft.TextStyle(size=BODY, weight=ft.FontWeight.BOLD),
            view_hint_text="Search name or paste an address",
            bar_shape=ft.RoundedRectangleBorder(radius=18),
            bar_elevation=0,
            bar_bgcolor=ft.Colors.TRANSPARENT,
            bar_border_side=ft.BorderSide(1, ft.Colors.OUTLINE_VARIANT),
            bar_trailing=[ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=20,
                                  color=ft.Colors.ON_SURFACE_VARIANT)],
            bar_size_constraints=ft.BoxConstraints(min_height=40, max_height=40),
            bar_padding=ft.Padding.only(left=10, right=4),
            width=PICKER_BAR_WIDTH,
            view_size_constraints=ft.BoxConstraints(
                min_width=PICKER_WIDTH, max_width=PICKER_WIDTH,
                max_height=PICKER_HEIGHT),
            view_shape=ft.RoundedRectangleBorder(radius=12),
            on_tap=self._opened,
            on_change=self._typed,
            on_tap_outside_bar=self._left,
            on_tap_outside_view=self._left,
        )

    # ------------------------------------------------------------ the coins

    def offer(self, entries: list[CoinEntry], chain: str | None = None) -> None:
        """The coins this chain has, busiest first."""
        if chain is not None:
            self._chain = chain
        self._entries = list(entries)
        if self._picked is not None:
            match = next((e for e in self._entries
                          if e.address == self._picked.address), None)
            self.pick(match, tell=False)
        self.controls = self._rows()

    @property
    def picked(self) -> CoinEntry | None:
        return self._picked

    def pick(self, entry: CoinEntry | None, *, tell: bool = True) -> None:
        self._picked = entry
        self.value = entry.symbol if entry else ""
        self.bar_leading = (
            ft.Container(token_mark(as_coin(entry), self._chain, MARK),
                         padding=ft.Padding.only(left=4))
            if entry else None
        )
        safe_update(self)
        if tell:
            self._on_pick(entry)

    # ---------------------------------------------------------- the control

    def _rows(self, query: str = "") -> list[ft.Control]:
        return [self._row(entry) for entry in matching_coins(self._entries, query)]

    def _row(self, entry: CoinEntry) -> ft.Control:
        return ft.ListTile(
            leading=token_mark(as_coin(entry), self._chain, MARK),
            title=ft.Text(entry.symbol, size=BODY, no_wrap=True),
            subtitle=ft.Text(entry.name or entry.address, size=LABEL,
                             color=ft.Colors.ON_SURFACE_VARIANT, no_wrap=True),
            dense=True,
            selected=self._picked is not None and entry.address == self._picked.address,
            on_click=lambda _e, chosen=entry: self._chose(chosen),
        )

    def _opened(self, _e: AnyEvent) -> None:
        """Open on an empty box, offering every coin.

        Opening is ours to do: a `SearchBar` reports the tap and waits.
        """
        self.value = ""
        self.controls = self._rows()
        safe_update(self)
        self._page.run_task(self.open_view)

    def _typed(self, event: AnyEvent) -> None:
        typed = getattr(event, "data", None)
        self.controls = self._rows(typed if isinstance(typed, str) else "")
        safe_update(self)

    def _left(self, _e: AnyEvent) -> None:
        """Dismissed without choosing: the coin is what it was."""
        self.value = self._picked.symbol if self._picked else ""
        self.controls = self._rows()
        safe_update(self)

    def _chose(self, entry: CoinEntry) -> None:
        # `close_view` waits on the client, so it is a task: this runs from a
        # click handler, which cannot await.  It closes on the text the bar
        # should show, which lands after `pick` has written it.
        self._page.run_task(self.close_view, entry.symbol)
        self.pick(entry)


class RouteDiagram(ft.Container):
    """The chosen route, drawn as flow through columns of tokens.

    A `cv.Canvas` for the ribbons and positioned controls for the labels: a
    canvas has no text-with-a-logo shape, and the marks are images.  The
    geometry is `ui.routegraph`, which is tested without a window.
    """

    def __init__(self, page: ft.Page, chain: str):
        self._page = page
        self._chain = chain
        self._diagram = None
        self._canvas = cv.Canvas(shapes=[], expand=True,
                                 on_resize=self._resized)
        self._labels = ft.Stack([], expand=True)
        self._empty = ft.Text(EMPTY_ROUTE, size=SMALL,
                              color=ft.Colors.ON_SURFACE_VARIANT,
                              text_align=ft.TextAlign.CENTER)
        self._size = (0.0, 0.0)
        super().__init__(
            ft.Stack([self._canvas, self._labels,
                      ft.Container(self._empty, alignment=ft.Alignment.CENTER)],
                     expand=True),
            padding=ft.Padding.all(14),
            border_radius=14,
            expand=True,
        )
        self.waiting = EMPTY_ROUTE

    def before_update(self) -> None:
        # A sheet of paper with the route drawn on it, rather than another
        # panel of controls: it is the one thing here that is a picture, and
        # `panel_shadow` is Chad-only -- in the Material themes the frame was
        # a rectangle of background with a hairline round it.
        self.shadow = theme.paper_shadow(self._page)
        self.border = theme.panel_border(self._page)
        self.bgcolor = theme.paper_bg(self._page)

    def show(self, diagram, chain: str | None = None) -> None:
        """Draw a route, or go back to waiting for one.

        The frame stays either way.  It used to appear with the first quote,
        which shifted the widget sideways the moment someone finished typing
        -- the one moment they are looking at it.
        """
        if chain is not None:
            self._chain = chain
        self._diagram = diagram
        self._empty.value = "" if diagram is not None else self.waiting
        self._draw()
        safe_update(self)

    def say(self, message: str) -> None:
        """No route to draw, and a reason for it."""
        self._diagram = None
        self._canvas.shapes = []
        self._labels.controls = []
        self._empty.value = message
        safe_update(self)

    def _resized(self, event) -> None:
        self._size = (getattr(event, "width", 0.0) or 0.0,
                      getattr(event, "height", 0.0) or 0.0)
        self._draw()
        safe_update(self._canvas)
        safe_update(self._labels)

    def _draw(self) -> None:
        width, height = self._size
        if self._diagram is None or width < 40 or height < 40:
            self._canvas.shapes = []
            self._labels.controls = []
            return
        got = routegraph.layout(self._diagram, width, height - routegraph.LABEL_HEIGHT)
        shapes: list[cv.Shape] = []
        for band in got.bands:
            shapes.append(cv.Path(
                _ribbon(band.points, band.height),
                paint=ft.Paint(color=_band_colour(band.colour),
                               style=ft.PaintingStyle.FILL),
            ))
        for bus in got.buses:
            shapes.append(cv.Rect(
                bus.x, bus.y, bus.width, bus.height, border_radius=3,
                paint=ft.Paint(
                    color=ft.Colors.PRIMARY if (bus.is_source or bus.is_dest)
                    else ft.Colors.ON_SURFACE_VARIANT,
                    style=ft.PaintingStyle.FILL),
            ))
        self._canvas.shapes = shapes
        self._labels.controls = self._label_row(got.buses, width)

    def _label_row(self, buses, width: float) -> list[ft.Control]:
        """The column names there is room to read, and no two on top of
        each other.

        Source and destination first, because they are what the trade is
        between and a diagram that leaves either out is answering a different
        question; the rest go left to right and give way to whatever has
        already claimed the space.  Claimed by the label's own box rather than
        by how far apart the columns are: the destination's label is nudged
        inside the frame, and against a fixed step that nudge silently landed
        it on its neighbour -- while three buses stacked in one column, whose
        labels sit at three different heights, all lost theirs to each other.
        """
        claimed: list[tuple[float, float, float, float]] = []
        drawn: list[ft.Control] = []
        order = sorted(buses, key=lambda b: (not (b.is_source or b.is_dest), b.x))
        for bus in order:
            box = _label_width(bus)
            left = min(max(0.0, bus.x + bus.width / 2 - box / 2),
                       max(0.0, width - box))
            top = bus.y + bus.height + LABEL_DROP
            span = (left - LABEL_AIR, top, left + box + LABEL_AIR,
                    top + routegraph.LABEL_HEIGHT)
            if any(_overlap(span, taken) for taken in claimed):
                continue
            claimed.append(span)
            drawn.append(self._label(bus, left, top, box))
        return drawn

    def _label(self, bus, left: float, top: float, box: float) -> ft.Control:
        """A column's token, under it, on a chip so it reads over a ribbon."""
        text = ft.Column(
            [
                ft.Text(bus.symbol, size=LABEL, no_wrap=True,
                        weight=ft.FontWeight.BOLD),
                ft.Text(_compact(bus.amount), size=LABEL - 1, no_wrap=True,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        chip = ft.Container(
            text,
            bgcolor=ft.Colors.with_opacity(0.78, theme.paper_bg(self._page)),
            border_radius=4,
            padding=ft.Padding.symmetric(vertical=1, horizontal=3),
            alignment=ft.Alignment.TOP_CENTER,
        )
        return ft.Container(chip, left=left, top=top, width=box,
                            alignment=ft.Alignment.TOP_CENTER)


def _overlap(one, two) -> bool:
    """Whether two label boxes share any ground."""
    return (one[0] < two[2] and two[0] < one[2]
            and one[1] < two[3] and two[1] < one[3])


def _label_width(bus) -> float:
    """Roughly how much room a column's label needs.

    Estimated from the text rather than measured: Flet reports a control's
    size only after it is laid out, and this decides whether to lay it out at
    all.  The factors are the widest a character of the theme's body font runs
    at `LABEL` -- generous on purpose, because an underestimate does not leave
    a label out, it clips one and sits it on its neighbour.
    """
    wide = max(len(bus.symbol) * LABEL * 0.78,
               len(_compact(bus.amount)) * (LABEL - 1) * 0.58)
    return min(LABEL_BOX, max(LABEL_MIN, wide + 6))


def _ribbon(points, height: float) -> list[cv.Path.PathElement]:
    """A leg as a closed shape: along the top edge, down, and back.

    Cubics between the points rather than lines, so a band that climbs or
    falls between columns reads as one ribbon rather than a staircase -- the
    shape Odos uses, and the reason to draw a path here rather than a
    rectangle.  A leg that spans several columns has a point on each side of
    every column it passes, and the horizontal pairs come out flat, which is
    what makes it look like it is threading between them rather than over.
    """
    top: list[cv.Path.PathElement] = [cv.Path.MoveTo(points[0][0], points[0][1])]
    for (x0, y0), (x1, y1) in pairwise(points):
        middle = (x0 + x1) / 2
        top.append(cv.Path.CubicTo(middle, y0, middle, y1, x1, y1))
    back: list[cv.Path.PathElement] = [
        cv.Path.LineTo(points[-1][0], points[-1][1] + height)]
    for (x1, y1), (x0, y0) in pairwise(points[::-1]):
        middle = (x0 + x1) / 2
        back.append(cv.Path.CubicTo(middle, y1 + height, middle, y0 + height,
                                    x0, y0 + height))
    return [*top, *back, cv.Path.Close()]


def _compact(amount: str) -> str:
    """A bus's amount, short enough to sit under a column.

    The router formats these to the wei -- "694,145.425897" -- which is right
    in a terminal where the number is the answer, and is a column and a half
    of clutter here where the *picture* is.  Two significant places and a
    suffix says the same thing at a glance.
    """
    try:
        value = float(amount.replace(",", ""))
    except (TypeError, ValueError):
        return amount
    for cut, suffix in ((1e9, "b"), (1e6, "m"), (1e3, "k")):
        if abs(value) >= cut:
            return f"{value / cut:,.2f}{suffix}".replace(".00", "")
    if value and abs(value) < 0.01:
        return f"{value:.6f}".rstrip("0")
    return f"{value:,.4f}".rstrip("0").rstrip(".")


#: A band's colour, by its position in the route.  Theme roles rather than
#: fixed hues, so the diagram is legible in all three themes -- and cycled,
#: because a thirteen-leg route has more legs than the palette has roles.
_BAND_COLOURS = (
    ft.Colors.PRIMARY,
    ft.Colors.TERTIARY,
    ft.Colors.SECONDARY,
    ft.Colors.PRIMARY_CONTAINER,
    ft.Colors.TERTIARY_CONTAINER,
    ft.Colors.SECONDARY_CONTAINER,
)


def _band_colour(index: int) -> str:
    """Enough colour to tell one leg from the next, and no more.

    These are ribbons over a sheet of paper, not areas of a chart: at full
    strength a route with two legs is two slabs, and the token names sitting
    under them stop being readable.
    """
    return ft.Colors.with_opacity(0.55, _BAND_COLOURS[index % len(_BAND_COLOURS)])


class SwapView(ft.Container):
    """The page: the widget, and the route beside it when there is room."""

    def __init__(self, page: ft.Page, chain: str, *, on_amount, on_pair,
                 on_max, on_approve, on_swap):
        self._page = page
        self.chain = chain
        self._layout: Layout | None = None
        self._stacked = False
        self._has_route = False
        self._on_amount = on_amount
        self._on_pair = on_pair
        self._on_max = on_max
        self._on_approve = on_approve
        self._on_swap = on_swap
        self._busy = False
        self._blocked = False
        self._empty = True

        self.sell = CoinPicker(page, chain, on_pick=self._picked, label="Sell")
        self.buy = CoinPicker(page, chain, on_pick=self._picked, label="Buy")

        self.max_button = ft.TextButton(
            "MAX", on_click=self._max_clicked,
            style=ft.ButtonStyle(
                padding=ft.Padding.symmetric(horizontal=8),
                text_style=ft.TextStyle(size=LABEL, weight=ft.FontWeight.BOLD)),
        )
        # MAX rides *inside* the field, as it does on the pool page -- beside
        # it the two halves of the trade were different widths, which reads as
        # a mistake rather than as a button.
        self.amount = ft.TextField(
            hint_text="0.0",
            dense=True,
            on_change=self._typed,
            expand=True,
            suffix_icon=self.max_button,
            suffix_icon_size_constraints=ft.BoxConstraints(
                min_width=58, min_height=28),
        )
        self.sell_balance = ft.Text("", size=LABEL,
                                    color=ft.Colors.ON_SURFACE_VARIANT)
        # A field rather than a `Text`, so the two halves of the trade are the
        # same shape; read-only because the router decides this number.
        self.receive = ft.TextField(
            hint_text="0.0", dense=True, read_only=True, value="", expand=True)
        self.buy_balance = ft.Text("", size=LABEL,
                                   color=ft.Colors.ON_SURFACE_VARIANT)
        self.reverse = ft.IconButton(
            ft.Icons.SWAP_VERT, tooltip="Swap direction",
            on_click=self._flip_clicked, icon_size=20)

        self.rows = _InfoRows()
        # Why a quoted route cannot be *sent*, which is a different thing
        # from a transaction going wrong and belongs next to the button
        # rather than in the status panel.
        self.blocked = ft.Text("", size=SMALL, color=ft.Colors.ERROR,
                               visible=False)
        self.status = StatusPanel(page)
        self.approve_button = buttons.Themed(
            "1. Approve spending", page=page, on_click=self._approve_clicked,
            visible=False, expand=True)
        self.submit_button = buttons.Themed(
            "Swap", page=page, on_click=self._swap_clicked, disabled=True,
            expand=True)

        self.diagram = RouteDiagram(page, chain)
        self.widget = ft.Container(
            ft.Column(
                [
                    ft.Text("Swap", size=TITLE, weight=ft.FontWeight.BOLD),
                    self._row(self.amount, self.sell, self.sell_balance),
                    ft.Row([self.reverse], alignment=ft.MainAxisAlignment.CENTER),
                    self._row(self.receive, self.buy, self.buy_balance),
                    self.rows.control,
                    self.approve_button,
                    self.submit_button,
                    self.blocked,
                    self.status,
                ],
                spacing=14,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            padding=ft.Padding.all(18),
            border_radius=14,
            width=WIDGET_WIDTH,
        )
        self._body = ft.Row(
            [self.widget],
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.START,
            spacing=24,
            wrap=False,
        )
        super().__init__(self._body, padding=ft.Padding.symmetric(vertical=18))

    def before_update(self) -> None:
        self.widget.shadow = theme.panel_shadow(self._page)
        self.widget.border = theme.panel_border(self._page)
        self.widget.bgcolor = ft.Colors.SURFACE_CONTAINER_LOW

    # ------------------------------------------------------------- assembly

    def _row(self, field: ft.Control, picker: CoinPicker,
             balance: ft.Text) -> ft.Control:
        """One half of the trade: the field, the coin, the balance under it.

        An ordinary bordered field beside the picker, the way the pool page's
        panels put theirs -- the tall tinted blocks this had were a third
        style in an app that already has one, and they made the widget twice
        the height it needs to be.  No "Sell"/"Buy" captions either: the
        arrow between them says which way round it is.
        """
        return ft.Column(
            [
                ft.Row(
                    [ft.Container(field, expand=True), picker],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
                ft.Row([balance], alignment=ft.MainAxisAlignment.END),
            ],
            spacing=2,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def set_layout(self, layout: Layout) -> None:
        """Draw the route beside the widget where there is room for it."""
        self._layout = layout
        self._stacked = layout.stacked
        self._sync_body()

    def _sync_body(self) -> None:
        """Where the widget sits, and whether the route sits beside it.

        Beside it whenever the screen is wide enough, route or no route: the
        frame appearing with the first quote moved the widget sideways at the
        exact moment someone had finished typing into it.
        """
        if self._stacked:
            # A phone has no room beside the widget, so the route goes under
            # it -- and the widget stops being 480 wide, because a fixed width
            # larger than the window is an overflow, and an overflowing
            # transform is what Flutter refuses to draw *at all*: the whole
            # page came back blank rather than clipped.
            self.widget.width = None
            self.diagram.width = None
            self.diagram.expand = False
            self.diagram.height = DIAGRAM_STACKED_HEIGHT
            # Hidden rather than absent until there is a route: taking it out
            # of the tree and putting it back is a change to the subtree the
            # amount field lives in, and updating that subtree sends the
            # server's copy of the field back to the browser -- over whatever
            # the reader has typed since.  The first quote of a session lands
            # mid-word, so that is exactly when it happened.
            self.diagram.visible = self._has_route
            self._body.controls = [
                ft.Column([self.widget, self.diagram], spacing=18,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                          expand=True)
            ]
        else:
            self.widget.width = WIDGET_WIDTH
            self.diagram.width = None
            self.diagram.expand = True
            self.diagram.height = DIAGRAM_HEIGHT
            self.diagram.visible = True
            self._body.controls = [self.widget, self.diagram]
        safe_update(self)

    # -------------------------------------------------------------- the pair

    def offer(self, entries, chain: str) -> None:
        """The coins this chain has."""
        self.chain = chain
        self.sell.offer(entries, chain)
        self.buy.offer(entries, chain)
        self.diagram._chain = chain

    def set_pair(self, sell: CoinEntry | None, buy: CoinEntry | None) -> None:
        self.sell.pick(sell, tell=False)
        self.buy.pick(buy, tell=False)
        self._sync_hints()

    @property
    def pair(self) -> tuple[CoinEntry | None, CoinEntry | None]:
        return self.sell.picked, self.buy.picked

    def amount_in(self) -> int:
        """What is in the box, in the sell coin's own units."""
        coin = self.sell.picked
        text = (self.amount.value or "").strip().replace(",", "")
        if coin is None or not text:
            return 0
        try:
            return parse_units(text, coin.decimals)
        except (ValueError, IndexError):
            return 0

    def show_balances(self, sell: int | None, buy: int | None) -> None:
        self.sell_balance.value = _balance_line(self.sell.picked, sell)
        self.buy_balance.value = _balance_line(self.buy.picked, buy)
        self.max_button.visible = bool(sell)
        safe_update(self.sell_balance)
        safe_update(self.buy_balance)
        safe_update(self.max_button)

    def fill_max(self, balance: int) -> None:
        """Sell the whole balance, to the last wei the token has.

        Every decimal, not a rounded figure: a MAX that leaves dust behind is
        a MAX that did not do what it said.
        """
        coin = self.sell.picked
        if coin is None:
            return
        self.amount.value = format_units(balance, coin.decimals,
                                         precision=coin.decimals)
        safe_update(self.amount)
        self._on_amount(self.amount_in())

    # ------------------------------------------------------------ the answer

    def show_quote(self, quote, plan=None) -> None:
        """The output, the figures under it, and the route."""
        sell, buy = self.sell.picked, self.buy.picked
        if quote is None or quote.result is None or quote.result.route is None:
            self.receive.value = ""
            self.rows.clear()
            self.diagram.show(None)
            self._set_has_route(False)
            self._empty = True
            self.submit_button.disabled = True
            self._sync()
            return
        result = quote.result
        out = result.verified_out or 0
        self.receive.value = (
            token_amount(out / 10 ** buy.decimals, places=6) if buy else "")
        self.rows.show(result, plan, sell, buy)
        self._empty = out <= 0
        self.submit_button.disabled = self._busy or self._blocked or self._empty
        self._sync()

    def show_route(self, diagram) -> None:
        self.diagram.show(diagram, self.chain)
        self._set_has_route(diagram is not None)

    def _set_has_route(self, has: bool) -> None:
        """Show or hide the stacked route, and touch nothing else.

        Only the frame is updated, never the widget above it: see
        `_sync_body`.
        """
        if has == self._has_route:
            return
        self._has_route = has
        if self._stacked:
            self.diagram.visible = has
            safe_update(self.diagram)

    def say(self, message: str, colour: str | None = None, *,
            pending: bool = False) -> None:
        self.status.say(message, colour, pending=pending)

    def clear_status(self) -> None:
        self.status.clear()

    def cannot_send(self, reason: str) -> None:
        """Say a quoted route cannot be sent, or take that back.

        The quote itself stays: it was verified on chain like any other, and
        the number is the answer to what was asked.  What is not on offer is
        the button.
        """
        self._blocked = bool(reason)
        self.blocked.value = reason
        self.blocked.visible = bool(reason)
        self.submit_button.disabled = self._busy or self._blocked or self._empty
        safe_update(self.blocked)
        safe_update(self.submit_button)

    def show_gas(self, text: str) -> None:
        """What the route costs to send, once there is a figure for it."""
        self.rows.set_gas(text)
        safe_update(self.rows.control)

    def show_approval(self, needed: bool) -> None:
        """Two steps or one, the way Curve's own widget puts it."""
        self.approve_button.visible = needed
        self.submit_button.content = "2. Swap" if needed else "Swap"
        safe_update(self.approve_button)
        safe_update(self.submit_button)

    def busy(self, busy: bool) -> None:
        """Nothing is clickable while a transaction is in flight.

        The flag outlives the call on purpose: a refresh landing mid-send
        would otherwise re-enable Submit under an already-built transaction,
        which is how a double-send happens.
        """
        self._busy = busy
        self.approve_button.disabled = busy
        self.submit_button.disabled = busy or self._blocked or self._empty
        self.amount.disabled = busy
        safe_update(self.approve_button)
        safe_update(self.submit_button)
        safe_update(self.amount)

    def clear_amount(self) -> None:
        self.amount.value = ""
        self.receive.value = ""
        self.rows.clear()
        self.diagram.show(None)
        self._sync()

    # ------------------------------------------------------------- handlers

    def _typed(self, _e: AnyEvent) -> None:
        self._on_amount(self.amount_in())

    def _max_clicked(self, _e: AnyEvent) -> None:
        self._on_max()

    def _flip_clicked(self, _e: AnyEvent) -> None:
        sell, buy = self.sell.picked, self.buy.picked
        self.sell.pick(buy, tell=False)
        self.buy.pick(sell, tell=False)
        self._sync_hints()
        self._on_pair(buy, sell)

    def _picked(self, _entry: CoinEntry | None) -> None:
        self._sync_hints()
        self._on_pair(self.sell.picked, self.buy.picked)

    def _approve_clicked(self, event: AnyEvent) -> None:
        self._page.run_task(self._on_approve)

    def _swap_clicked(self, event: AnyEvent) -> None:
        self._page.run_task(self._on_swap)

    def _sync_hints(self) -> None:
        self.sell.bar_hint_text = "Sell"
        self.buy.bar_hint_text = "Buy"
        safe_update(self.sell)
        safe_update(self.buy)

    def _sync(self) -> None:
        safe_update(self.receive)
        safe_update(self.submit_button)
        safe_update(self.rows.control)


class _InfoRows:
    """The figures under the amounts: what this trade costs and promises."""

    def __init__(self) -> None:
        self._rows: dict[str, ft.Text] = {}
        lines: list[ft.Control] = []
        for key, label in (
            ("slippage", "Slippage"),
            ("impact", "Price impact"),
            ("rate", "Exchange rate"),
            ("route", "Trade route"),
            ("gas", "Estimated tx cost"),
        ):
            value = ft.Text("-", size=SMALL, no_wrap=True,
                            color=ft.Colors.ON_SURFACE_VARIANT)
            self._rows[key] = value
            lines.append(ft.Row(
                [ft.Text(label, size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT),
                 value],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ))
        self.control = ft.Column(lines, spacing=3)

    def clear(self) -> None:
        for value in self._rows.values():
            value.value = "-"
            value.color = ft.Colors.ON_SURFACE_VARIANT

    def set_gas(self, text: str) -> None:
        self._set("gas", text)

    def show(self, result, plan, sell=None, buy=None) -> None:
        pools, legs = routegraph.summarise(_diagram_of(result))
        self._set("route", f"{pools} pool{'s' if pools != 1 else ''} · "
                           f"{legs} leg{'s' if legs != 1 else ''}")
        impact = float(getattr(result, "price_impact_bp", 0.0) or 0.0)
        self._set("impact", f"{impact:.2f} bp",
                  ft.Colors.ERROR if impact >= IMPACT_HIGH_BP else None)
        self._set("rate", _rate_line(result, sell, buy))
        # Just "auto".  The bound is *per leg*, derived from the least each
        # pool can charge, so a single figure for a fifteen-pool route is a
        # number that describes none of them -- and the compounded total it
        # used to show read like a slippage setting, which it is not.
        self._set("slippage", "auto")
        if plan is None or not plan.gas:
            # No figure: the route did not execute locally, which for an
            # unapproved token is the expected answer and not a fault.
            self._set("gas", "-")

    def _set(self, key: str, text: str, colour: str | None = None) -> None:
        row = self._rows[key]
        row.value = text or "-"
        row.color = colour or ft.Colors.ON_SURFACE_VARIANT


def _rate_line(result, sell, buy) -> str:
    """What one of the sell coin buys, which is the figure people compare."""
    out = getattr(result, "verified_out", 0) or 0
    amount_in = getattr(result, "amount_in", 0) or 0
    if not (out and amount_in and sell and buy):
        return ""
    rate = (out / 10 ** buy.decimals) / (amount_in / 10 ** sell.decimals)
    return f"1 {sell.symbol} = {token_amount(rate, places=6)} {buy.symbol}"


def _diagram_of(result):
    """The `Diagram` for a result, or something `summarise` can count."""
    return getattr(result, "diagram", None) or _Elements(result)


class _Elements:
    """Enough of a `Diagram` to count pools and legs off a raw route."""

    def __init__(self, result) -> None:
        route = getattr(result, "route", None)
        self.elements = [
            type("E", (), {"target": leg.target})()
            for leg in (getattr(route, "legs", ()) or ())
        ]


def _balance_line(coin: CoinEntry | None, balance: int | None) -> str:
    if coin is None or balance is None:
        return ""
    return f"Balance: {format_units(balance, coin.decimals, precision=6)} {coin.symbol}"
