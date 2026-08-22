"""Laying out a route as a diagram, with no Flet in the arithmetic.

The router hands over a structured `Diagram` -- buses (a token rail), elements
(a leg, drawn as a diode in series with a resistor), and the order the buses
come in.  Its own `render_text` turns that into box-drawing characters; this
turns the same model into rectangles, and `swap.py` turns those into canvas
shapes.

Kept apart from the drawing for the reason the chart's `viewport.py` is: the
geometry is where the mistakes are -- a band that overlaps its neighbour, a
column that runs off the edge, a share that does not add up -- and none of
that needs a window to find.

The shape is Odos's, because it is the one that shows what this router
actually does: flow *splits*, and a picture of a single path would be a
picture of the special case.  A bus is a column, a leg is a band between two
columns, and the band's height is its share of the flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How wide a bus column is drawn.
BUS_WIDTH = 12.0

#: The gap between two stacked bands on one bus, so a split reads as several
#: bands rather than one tall one.
BAND_GAP = 2.0

#: What a band is never thinner than.  A leg carrying 0.4% of the flow is
#: still a leg someone may want to see; below a couple of pixels it is a line
#: nobody can tell from the gap above it.
MIN_BAND = 3.0

#: Room left under the columns for their labels.
LABEL_HEIGHT = 26.0


@dataclass(frozen=True, slots=True)
class BusBox:
    """One token rail, as a column."""

    slot: int
    symbol: str
    amount: str
    x: float
    y: float
    width: float
    height: float
    is_source: bool = False
    is_dest: bool = False

    @property
    def middle(self) -> float:
        return self.y + self.height / 2


@dataclass(frozen=True, slots=True)
class Band:
    """One leg, as a ribbon from one column to the next."""

    index: int
    label: str
    kind: str
    share: float
    #: Where it leaves and where it arrives.  Two rectangles rather than one,
    #: because a leg that skips a column has to cross it.
    x0: float
    y0: float
    x1: float
    y1: float
    height: float
    colour: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Layout:
    """A whole diagram, positioned."""

    buses: list[BusBox] = field(default_factory=list)
    bands: list[Band] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0

    @property
    def columns(self) -> int:
        return len({bus.x for bus in self.buses})


def layout(diagram, width: float, height: float, *, bus_width: float = BUS_WIDTH,
           gap: float = BAND_GAP, min_band: float = MIN_BAND) -> Layout:
    """Position a `Diagram` inside `width` x `height`.

    Columns are the buses in the order the router put them, evenly spaced;
    a band's height is its share of what leaves its source bus, and a bus is
    as tall as the bands touching it.
    """
    order = list(getattr(diagram, "order", ()) or ())
    buses = {bus.slot: bus for bus in getattr(diagram, "buses", ()) or ()}
    elements = list(getattr(diagram, "elements", ()) or ())
    if not order or not elements:
        return Layout(width=width, height=height)

    # A bus is drawn in the first column its slot appears in, so a leg always
    # runs left to right and a merge lands on one column rather than two.
    column_of = {slot: k for k, slot in enumerate(order)}
    span = max(1, len(order) - 1)
    step = (width - bus_width) / span if span else 0.0
    drawable = max(1.0, height - LABEL_HEIGHT)

    weights = flows(diagram)
    # The gaps come out of the height *before* the bands are sized, so a bus
    # carrying the whole flow -- the source always does -- is as tall as its
    # bands and their gaps together, and no taller than the space there is.
    parallel = _busiest(elements)
    usable = max(min_band, drawable - gap * max(0, parallel - 1))
    heights = {
        element.index: max(min_band, usable * weights.get(element.index, 0.0))
        for element in elements
    }

    # A bus is as tall as the bands on its busier side, gaps included -- so a
    # split's bands add up to the node they leave, exactly, whatever the dust
    # floor did to the smallest of them.
    stacks: dict[int, dict[str, list[float]]] = {}
    for element in elements:
        tall = heights[element.index]
        stacks.setdefault(element.src_slot, {"out": [], "in": []})["out"].append(tall)
        stacks.setdefault(element.dst_slot, {"out": [], "in": []})["in"].append(tall)

    boxes: dict[int, BusBox] = {}
    for slot in order:
        bus = buses.get(slot)
        sides = stacks.get(slot) or {"out": [], "in": []}
        tall = max(_stack(sides["out"], gap), _stack(sides["in"], gap), min_band)
        boxes[slot] = BusBox(
            slot=slot,
            symbol=getattr(bus, "symbol", "") or "",
            amount=getattr(bus, "amount", "") or "",
            x=column_of[slot] * step,
            y=(drawable - tall) / 2,
            width=bus_width,
            height=tall,
            is_source=bool(getattr(bus, "is_source", False)),
            is_dest=bool(getattr(bus, "is_dest", False)),
        )

    # Bands stack down each bus in the order the router listed them, on both
    # sides, so a leg leaves where the one before it stopped.
    leaving = {slot: box.y for slot, box in boxes.items()}
    arriving = {slot: box.y for slot, box in boxes.items()}
    bands: list[Band] = []
    for element in elements:
        source, target = boxes.get(element.src_slot), boxes.get(element.dst_slot)
        if source is None or target is None:
            continue
        tall = heights[element.index]
        y0, y1 = leaving[element.src_slot], arriving[element.dst_slot]
        bands.append(Band(
            index=element.index,
            label=getattr(element, "label", "") or "",
            kind=getattr(element.kind, "name", str(element.kind)),
            share=float(element.share_pct),
            x0=source.x + source.width,
            y0=y0,
            x1=target.x,
            y1=y1,
            height=tall,
            colour=element.index,
            detail=getattr(element, "detail", "") or "",
        ))
        leaving[element.src_slot] = y0 + tall + gap
        arriving[element.dst_slot] = y1 + tall + gap
    return Layout(list(boxes.values()), bands, width, height)


def _busiest(elements) -> int:
    """The most bands that ever meet on one side of one bus."""
    sides: dict[tuple[int, str], int] = {}
    for element in elements:
        sides[(element.src_slot, "out")] = sides.get((element.src_slot, "out"), 0) + 1
        sides[(element.dst_slot, "in")] = sides.get((element.dst_slot, "in"), 0) + 1
    return max(sides.values() or [1])


def _stack(heights: list[float], gap: float) -> float:
    """How tall a column of bands is, gaps included."""
    if not heights:
        return 0.0
    return sum(heights) + gap * (len(heights) - 1)


def flows(diagram) -> dict[int, float]:
    """Each leg's share of the *whole* route, by element index.

    `share_pct` is a share of what leaves that leg's own node, which is what
    the router means by a split: two legs out of the source read 60 and 40, and
    two legs out of a node further along read 100 between them.  Drawn as they
    come, a leg deep in the route looks as important as the whole trade.

    So the flow is carried forward.  The bus order is topological -- a node's
    inflows all precede its outflows, which is the same property the router's
    own `fractions` relies on to execute -- so one pass in the listed order is
    enough.
    """
    elements = list(getattr(diagram, "elements", ()) or ())
    if not elements:
        return {}
    sources = {bus.slot for bus in getattr(diagram, "buses", ()) or ()
               if getattr(bus, "is_source", False)}
    if not sources:
        order = list(getattr(diagram, "order", ()) or ())
        sources = {order[0]} if order else {elements[0].src_slot}
    held: dict[int, float] = dict.fromkeys(sources, 1.0)
    out: dict[int, float] = {}
    for element in elements:
        share = max(0.0, float(element.share_pct)) / 100.0
        weight = held.get(element.src_slot, 0.0) * share
        out[element.index] = weight
        held[element.dst_slot] = held.get(element.dst_slot, 0.0) + weight
    return out


def summarise(diagram) -> tuple[int, int]:
    """`(pools, legs)` -- what the widget says under the amounts.

    Pools rather than legs, counted distinctly: a route that uses one pool
    twice is two legs through one market, and someone reading "3 pools" and
    counting three names is not being told something different.
    """
    elements = list(getattr(diagram, "elements", ()) or ())
    pools = {e.target.lower() for e in elements if getattr(e, "target", "")}
    return len(pools), len(elements)
