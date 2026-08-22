"""Laying out a route as a diagram, with no Flet in the arithmetic.

The router hands over a structured `Diagram` -- buses (a token rail), elements
(a leg), and the order the buses come in.  Its own `render_text` turns that
into box-drawing characters; this turns the same model into rectangles, and
`swap.py` turns those into canvas shapes.

Kept apart from the drawing for the reason the chart's `viewport.py` is: the
geometry is where the mistakes are -- a band that overlaps its neighbour, a
column that runs off the edge, a share that does not add up -- and none of
that needs a window to find.

The shape is Odos's, because it is the one that shows what this router
actually does: flow *splits*, and a picture of a single path would be a
picture of the special case.

**Columns are layers, not list positions.**  A route is a DAG, not a chain:
its buses arrive in an order that says nothing about how far along each one
is, so putting bus `k` in column `k` let a leg run backwards and drew one
ribbon straight over another.  Each bus goes in the column of its *longest*
path from the source, which is what makes every leg run strictly left to
right.  Within a column the buses are ordered by where their own sources sit,
and each bus's ribbons leave and arrive in the order of the bus at the other
end -- which is what stops them crossing over each other at a node.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: How wide a bus column is drawn.
BUS_WIDTH = 10.0

#: Between two buses stacked in one column -- and it is this wide because a
#: bus carries its name and its amount underneath, which have to fit.
NODE_GAP = 30.0

#: What a band is never thinner than.  A leg carrying 0.4% of the flow is
#: still a leg someone may want to see; below a couple of pixels it is a line
#: nobody can tell from the gap above it.
MIN_BAND = 2.5

#: Room under the lowest bus for its label.
LABEL_HEIGHT = 26.0

#: How much of the frame the ribbons are allowed to fill.  Not all of it: a
#: one-leg route carries 100% of the flow and would be drawn as a rectangle of
#: colour touching the frame on every side, which reads as a swatch rather
#: than as a picture of anything.
FILL = 0.82


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
    layer: int = 0
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
        return len({bus.layer for bus in self.buses})


def layout(diagram, width: float, height: float, *, bus_width: float = BUS_WIDTH,
           gap: float = NODE_GAP, min_band: float = MIN_BAND) -> Layout:
    """Position a `Diagram` inside `width` x `height`."""
    elements = list(getattr(diagram, "elements", ()) or ())
    buses = {bus.slot: bus for bus in getattr(diagram, "buses", ()) or ()}
    if not elements:
        return Layout(width=width, height=height)

    depth = layers(diagram)
    columns = max(depth.values()) + 1
    span = max(1, columns - 1)
    step = (width - bus_width) / span if span else 0.0
    drawable = max(1.0, height - LABEL_HEIGHT)

    weight = flows(diagram)
    through = _through(elements, weight)
    by_layer: dict[int, list[int]] = {}
    for slot, level in depth.items():
        by_layer.setdefault(level, []).append(slot)

    # Height first: a column's buses and the gaps between them together fill
    # the frame, so the busiest column is what everything is scaled against.
    crowd = max(len(slots) for slots in by_layer.values())
    usable = max(min_band, drawable * FILL - gap * max(0, crowd - 1))
    busiest = max((sum(through.get(s, 0.0) for s in slots)
                   for slots in by_layer.values()), default=1.0) or 1.0
    scale = usable / busiest

    boxes: dict[int, BusBox] = {}
    for level in sorted(by_layer):
        slots = _ordered(by_layer[level], elements, boxes)
        heights = [max(min_band, through.get(slot, 0.0) * scale) for slot in slots]
        tall = sum(heights) + gap * (len(heights) - 1)
        top = max(0.0, (drawable - tall) / 2)
        for slot, high in zip(slots, heights, strict=True):
            bus = buses.get(slot)
            boxes[slot] = BusBox(
                slot=slot,
                symbol=getattr(bus, "symbol", "") or "",
                amount=getattr(bus, "amount", "") or "",
                x=level * step,
                y=top,
                width=bus_width,
                height=high,
                layer=level,
                is_source=bool(getattr(bus, "is_source", False)),
                is_dest=bool(getattr(bus, "is_dest", False)),
            )
            top += high + gap

    bands = _ribbons(elements, boxes, weight, scale, min_band)
    return Layout(list(boxes.values()), bands, width, height)


def layers(diagram) -> dict[int, int]:
    """Which column each bus belongs in: its longest path from the source.

    Longest rather than shortest, because that is what guarantees every leg
    runs strictly forward -- a bus reached both directly and through two
    others has to sit past both of them, or the direct leg draws backwards
    over the top of them.

    Over a topological order of the buses rather than over the list of legs.
    The list is ordered for *execution* -- a node's inflows precede its
    outflows -- and reading it directly puts a token in the column before the
    token it is made from the moment that order differs.

    The order is found here rather than assumed, and it has to be, because the
    bus graph is not always acyclic: a route may sell a token it bought back,
    and the router says so ("4 pool(s) used more than once").  A leg that
    closes a cycle is left out of the layering -- it still gets drawn, as a
    ribbon running the length of the picture -- because relaxing over one
    pushes every bus rightwards until it hits whatever bound is put on the
    loop, which crushed the whole diagram into the last fifth of its frame.
    """
    elements = list(getattr(diagram, "elements", ()) or ())
    after: dict[int, list[int]] = {}
    into: dict[int, int] = {}
    for element in elements:
        after.setdefault(element.src_slot, []).append(element.dst_slot)
        after.setdefault(element.dst_slot, [])
        into.setdefault(element.src_slot, 0)
        into[element.dst_slot] = into.get(element.dst_slot, 0) + 1

    ready = [slot for slot, count in into.items() if count == 0]
    order: list[int] = []
    while ready:
        slot = ready.pop(0)
        order.append(slot)
        for onward in after.get(slot, ()):
            into[onward] -= 1
            if into[onward] == 0:
                ready.append(onward)
    # Whatever a cycle held back goes last, in the order the router listed it.
    seen = set(order)
    order += [slot for slot in after if slot not in seen]

    depth = dict.fromkeys(after, 0)
    place = {slot: k for k, slot in enumerate(order)}
    for slot in order:
        for onward in after.get(slot, ()):
            if place[onward] > place[slot]:
                depth[onward] = max(depth[onward], depth[slot] + 1)
    return depth


def flows(diagram) -> dict[int, float]:
    """Each leg's share of the *whole* route, by element index.

    `share_pct` is a share of what leaves that leg's own node, which is what
    the router means by a split: two legs out of the source read 60 and 40, and
    two legs out of a node further along read 100 between them.  Drawn as they
    come, a leg deep in the route looks as important as the whole trade.
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
        carried = held.get(element.src_slot, 0.0) * share
        out[element.index] = carried
        held[element.dst_slot] = held.get(element.dst_slot, 0.0) + carried
    return out


def _through(elements, weight: dict[int, float]) -> dict[int, float]:
    """How much of the route passes through each bus."""
    incoming: dict[int, float] = {}
    outgoing: dict[int, float] = {}
    for element in elements:
        carried = weight.get(element.index, 0.0)
        outgoing[element.src_slot] = outgoing.get(element.src_slot, 0.0) + carried
        incoming[element.dst_slot] = incoming.get(element.dst_slot, 0.0) + carried
    slots = set(incoming) | set(outgoing)
    return {slot: max(incoming.get(slot, 0.0), outgoing.get(slot, 0.0))
            for slot in slots}


def _ordered(slots: list[int], elements, placed: dict[int, BusBox]) -> list[int]:
    """A column's buses, top to bottom, near whatever feeds them.

    One barycentre pass: a bus sits at the average height of the buses already
    placed that feed it.  It is not optimal ordering -- that is NP-hard -- and
    it is most of the difference between ribbons that cross and ribbons that
    do not.
    """
    def key(slot: int) -> tuple[float, int]:
        feeders = [placed[e.src_slot].middle for e in elements
                   if e.dst_slot == slot and e.src_slot in placed]
        return (sum(feeders) / len(feeders) if feeders else 0.0, slot)

    return sorted(slots, key=key)


def _ribbons(elements, boxes: dict[int, BusBox], weight: dict[int, float],
             scale: float, min_band: float) -> list[Band]:
    """Where each leg leaves and where it arrives.

    A bus's ribbons are ordered by the bus at the *other* end, on both sides,
    so two legs sharing a node do not swap places crossing it.
    """
    leaving: dict[int, float] = {slot: box.y for slot, box in boxes.items()}
    arriving: dict[int, float] = {slot: box.y for slot, box in boxes.items()}

    out_order: dict[int, list] = {}
    in_order: dict[int, list] = {}
    for element in elements:
        out_order.setdefault(element.src_slot, []).append(element)
        in_order.setdefault(element.dst_slot, []).append(element)
    for group in out_order.values():
        group.sort(key=lambda e: boxes[e.dst_slot].middle if e.dst_slot in boxes else 0.0)
    for group in in_order.values():
        group.sort(key=lambda e: boxes[e.src_slot].middle if e.src_slot in boxes else 0.0)

    starts: dict[int, float] = {}
    for slot, group in out_order.items():
        for element in group:
            tall = max(min_band, weight.get(element.index, 0.0) * scale)
            starts[element.index] = leaving[slot]
            leaving[slot] += tall
    ends: dict[int, float] = {}
    for slot, group in in_order.items():
        for element in group:
            tall = max(min_band, weight.get(element.index, 0.0) * scale)
            ends[element.index] = arriving[slot]
            arriving[slot] += tall

    bands: list[Band] = []
    for element in elements:
        source, target = boxes.get(element.src_slot), boxes.get(element.dst_slot)
        if source is None or target is None:
            continue
        bands.append(Band(
            index=element.index,
            label=getattr(element, "label", "") or "",
            kind=getattr(element.kind, "name", str(element.kind)),
            share=float(element.share_pct),
            x0=source.x + source.width,
            y0=starts[element.index],
            x1=target.x,
            y1=ends[element.index],
            height=max(min_band, weight.get(element.index, 0.0) * scale),
            colour=element.index,
            detail=getattr(element, "detail", "") or "",
        ))
    return bands


def summarise(diagram) -> tuple[int, int]:
    """`(pools, legs)` -- what the widget says under the amounts.

    Pools rather than legs, counted distinctly: a route that uses one pool
    twice is two legs through one market, and someone reading "3 pools" and
    counting three names is not being told something different.
    """
    elements = list(getattr(diagram, "elements", ()) or ())
    pools = {e.target.lower() for e in elements if getattr(e, "target", "")}
    return len(pools), len(elements)
