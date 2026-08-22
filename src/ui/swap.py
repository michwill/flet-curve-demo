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

#: Below this the diagram is not drawn at all: a route needs width to be
#: legible, and a phone has none to spare beside the widget.
DIAGRAM_MIN_WIDTH = 420.0

#: How tall the drawn route is.
DIAGRAM_HEIGHT = 320.0

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
        self._empty = ft.Text("", size=SMALL, color=ft.Colors.ON_SURFACE_VARIANT,
                              text_align=ft.TextAlign.CENTER)
        self._size = (0.0, 0.0)
        super().__init__(
            ft.Stack([self._canvas, self._labels,
                      ft.Container(self._empty, alignment=ft.Alignment.CENTER)],
                     expand=True),
            height=DIAGRAM_HEIGHT,
            padding=ft.Padding.all(12),
            border_radius=12,
            visible=False,
        )

    def before_update(self) -> None:
        self.shadow = theme.panel_shadow(self._page)
        self.border = theme.panel_border(self._page)

    def show(self, diagram, chain: str | None = None) -> None:
        if chain is not None:
            self._chain = chain
        self._diagram = diagram
        self.visible = diagram is not None
        self._empty.value = ""
        self._draw()
        safe_update(self)

    def say(self, message: str) -> None:
        """No route to draw, and a reason."""
        self._diagram = None
        self.visible = True
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
                [
                    cv.Path.MoveTo(band.x0, band.y0),
                    # A cubic through the midpoint, so a band that climbs or
                    # falls between columns reads as one ribbon rather than a
                    # staircase -- the shape Odos uses, and the reason to use
                    # a path here rather than a rectangle.
                    cv.Path.CubicTo((band.x0 + band.x1) / 2, band.y0,
                                    (band.x0 + band.x1) / 2, band.y1,
                                    band.x1, band.y1),
                    cv.Path.LineTo(band.x1, band.y1 + band.height),
                    cv.Path.CubicTo((band.x0 + band.x1) / 2, band.y1 + band.height,
                                    (band.x0 + band.x1) / 2, band.y0 + band.height,
                                    band.x0, band.y0 + band.height),
                    cv.Path.Close(),
                ],
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
        self._labels.controls = [self._label(bus, width) for bus in got.buses]

    def _label(self, bus, width: float) -> ft.Control:
        """A column's token, under it, kept inside the frame at either edge."""
        text = ft.Column(
            [
                ft.Text(bus.symbol, size=LABEL, no_wrap=True,
                        weight=ft.FontWeight.BOLD),
                ft.Text(bus.amount, size=LABEL - 1, no_wrap=True,
                        color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
        box = 96.0
        left = min(max(0.0, bus.x + bus.width / 2 - box / 2), max(0.0, width - box))
        return ft.Container(text, left=left, top=bus.y + bus.height + 6, width=box,
                            alignment=ft.Alignment.TOP_CENTER)


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
    return ft.Colors.with_opacity(0.85, _BAND_COLOURS[index % len(_BAND_COLOURS)])


class SwapView(ft.Container):
    """The page: the widget, and the route beside it when there is room."""

    def __init__(self, page: ft.Page, chain: str, *, on_amount, on_pair,
                 on_max, on_approve, on_swap):
        self._page = page
        self.chain = chain
        self._layout: Layout | None = None
        self._stacked = False
        self._on_amount = on_amount
        self._on_pair = on_pair
        self._on_max = on_max
        self._on_approve = on_approve
        self._on_swap = on_swap
        self._busy = False

        self.sell = CoinPicker(page, chain, on_pick=self._picked, label="Sell")
        self.buy = CoinPicker(page, chain, on_pick=self._picked, label="Buy")

        self.amount = ft.TextField(
            hint_text="0.00",
            border=ft.InputBorder.NONE,
            text_size=28,
            content_padding=ft.Padding.symmetric(vertical=4),
            on_change=self._typed,
            expand=True,
        )
        self.max_button = ft.TextButton(
            "MAX", on_click=self._max_clicked,
            style=ft.ButtonStyle(
                padding=ft.Padding.symmetric(horizontal=8),
                text_style=ft.TextStyle(size=LABEL, weight=ft.FontWeight.BOLD)),
        )
        self.sell_balance = ft.Text("", size=LABEL,
                                    color=ft.Colors.ON_SURFACE_VARIANT)
        self.receive = ft.Text("0.00", size=28, no_wrap=True)
        self.buy_balance = ft.Text("", size=LABEL,
                                   color=ft.Colors.ON_SURFACE_VARIANT)
        self.reverse = ft.IconButton(
            ft.Icons.SWAP_VERT, tooltip="Swap direction",
            on_click=self._flip_clicked, icon_size=20)

        self.rows = _InfoRows()
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
                    self._box("Sell", self.amount, self.sell,
                              self.sell_balance, self.max_button),
                    ft.Row([self.reverse], alignment=ft.MainAxisAlignment.CENTER),
                    self._box("Buy", self.receive, self.buy, self.buy_balance),
                    self.rows.control,
                    self.approve_button,
                    self.submit_button,
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

    def _box(self, label: str, value: ft.Control, picker: CoinPicker,
             balance: ft.Text, trailing: ft.Control | None = None) -> ft.Control:
        """One half of the trade: a label, a big number, and the coin."""
        line: list[ft.Control] = [ft.Container(value, expand=True)]
        if trailing is not None:
            line.append(trailing)
        return ft.Container(
            ft.Column(
                [
                    ft.Text(label, size=LABEL, color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Row(
                        [ft.Row(line, expand=True, spacing=4,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER),
                         picker],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=8,
                    ),
                    ft.Row([balance], alignment=ft.MainAxisAlignment.END),
                ],
                spacing=2,
            ),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border_radius=12,
            bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )

    def set_layout(self, layout: Layout) -> None:
        """Draw the route beside the widget where there is room for it."""
        self._layout = layout
        self._stacked = layout.stacked
        if layout.stacked:
            # A phone has no room beside the widget, so the route goes under
            # it -- and the widget stops being 480 wide, because a fixed width
            # larger than the window is an overflow, and an overflowing
            # transform is what Flutter refuses to draw *at all*: the whole
            # page came back blank rather than clipped.
            self.widget.width = None
            self.diagram.width = None
            self._body.controls = [
                ft.Column([self.widget, self.diagram], spacing=18,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                          expand=True)
            ]
        else:
            self.widget.width = WIDGET_WIDTH
            self.diagram.width = DIAGRAM_MIN_WIDTH
            self._body.controls = [
                self.widget,
                ft.Container(self.diagram, padding=ft.Padding.only(top=44)),
            ]
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
            self.receive.value = "0.00"
            self.rows.clear()
            self.diagram.show(None)
            self.submit_button.disabled = True
            self._sync()
            return
        result = quote.result
        out = result.verified_out or 0
        self.receive.value = (
            token_amount(out / 10 ** buy.decimals, places=6) if buy else "0.00")
        self.rows.show(result, quote, plan, sell, buy)
        self.submit_button.disabled = self._busy or out <= 0
        self._sync()

    def show_route(self, diagram) -> None:
        self.diagram.show(diagram, self.chain)

    def say(self, message: str, colour: str | None = None, *,
            pending: bool = False) -> None:
        self.status.say(message, colour, pending=pending)

    def clear_status(self) -> None:
        self.status.clear()

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
        self.submit_button.disabled = busy
        self.amount.disabled = busy
        safe_update(self.approve_button)
        safe_update(self.submit_button)
        safe_update(self.amount)

    def clear_amount(self) -> None:
        self.amount.value = ""
        self.receive.value = "0.00"
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
            ("note", ""),
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

    def show(self, result, quote, plan, sell=None, buy=None) -> None:
        pools, legs = routegraph.summarise(_diagram_of(result))
        self._set("route", f"{pools} pool{'s' if pools != 1 else ''} · "
                           f"{legs} leg{'s' if legs != 1 else ''}")
        impact = float(getattr(result, "price_impact_bp", 0.0) or 0.0)
        self._set("impact", f"{impact:.2f} bp",
                  ft.Colors.ERROR if impact >= IMPACT_HIGH_BP else None)
        self._set("rate", _rate_line(result, sell, buy))
        if plan is not None:
            # "auto" is the router's own per-leg bound, not a number someone
            # typed: each leg carries a minimum rate derived from the least
            # its own pool can charge, and this is what they add up to.
            self._set("slippage", f"auto · {plan.tolerance_bp:.2f} bp")
            if not plan.gas:
                # No figure: the route did not execute locally, which for an
                # unapproved token is the expected answer and not a fault.
                self._set("gas", "-")
        else:
            self._set("slippage", "auto")
            self._set("gas", "-")
        self._set("note", _certificate_note(result, quote))

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


def _certificate_note(result, quote) -> str:
    """What the solver would not promise, when it would not promise it.

    A false certificate is not a wrong answer: it is the solver saying it
    could not *prove* this route optimal, which happens when the search hits
    its pivot limit or cycles.  The quote itself is still the chain's own
    number, verified on chain like every other.

    Shown rather than hidden, because the whole diagnostic layer in the router
    exists for one reason -- a certificate that is false and swallowed is the
    failure mode it was built to prevent.
    """
    reason = getattr(result, "certificate_reason", None)
    if getattr(result, "certificate", False) or not reason:
        return ""
    return f"not proven optimal · {reason.lower().replace('_', ' ')}"


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
