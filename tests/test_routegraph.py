"""The route diagram's geometry, which is where its mistakes are.

A ribbon that overlaps its neighbour, a column off the edge, shares that do
not add up -- none of that needs a window to find, which is why the arithmetic
is a module of its own.

The one that got out: buses were put in the column of their position in the
router's list, which says nothing about how far along a bus is.  A leg then
ran backwards and drew one ribbon straight over another.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ui.routegraph import MIN_BAND, NODE_GAP, layers, layout, summarise


@dataclass
class FakeBus:
    slot: int
    symbol: str = ""
    amount: str = ""
    is_source: bool = False
    is_dest: bool = False


@dataclass
class FakeKind:
    name: str = "SWAP_STABLE"


@dataclass
class FakeElement:
    index: int
    src_slot: int
    dst_slot: int
    share_pct: float
    label: str = ""
    target: str = "0xpool"
    detail: str = ""
    kind: FakeKind = field(default_factory=FakeKind)


@dataclass
class FakeDiagram:
    buses: list = field(default_factory=list)
    elements: list = field(default_factory=list)
    order: list = field(default_factory=list)


def straight() -> FakeDiagram:
    """One leg, source to destination."""
    return FakeDiagram(
        buses=[FakeBus(0, "USDC", "1,000", is_source=True),
               FakeBus(1, "WETH", "0.41", is_dest=True)],
        elements=[FakeElement(0, 0, 1, 100.0, "3pool", "0xaaa")],
        order=[0, 1],
    )


def split() -> FakeDiagram:
    """Two legs out of the source, merging on the destination."""
    return FakeDiagram(
        buses=[FakeBus(0, "USDC", "1,000", is_source=True),
               FakeBus(1, "crvUSD", "999"),
               FakeBus(2, "WETH", "0.41", is_dest=True)],
        elements=[
            FakeElement(0, 0, 1, 60.0, "a", "0xaaa"),
            FakeElement(1, 0, 2, 40.0, "b", "0xbbb"),
            FakeElement(2, 1, 2, 100.0, "c", "0xccc"),
        ],
        order=[0, 1, 2],
    )


def test_an_empty_diagram_lays_out_to_nothing():
    got = layout(FakeDiagram(), 400, 200)
    assert got.buses == [] and got.bands == []
    assert (got.width, got.height) == (400, 200)


def test_the_columns_span_the_width_and_stay_inside_it():
    got = layout(split(), 400, 200)
    assert got.columns == 3
    assert min(bus.x for bus in got.buses) == 0
    assert max(bus.x + bus.width for bus in got.buses) == 400


def test_every_leg_runs_left_to_right():
    """The regression. A bus put in the column of its list position let a leg
    run backwards, which draws one ribbon over another."""
    got = layout(split(), 400, 300)
    for band in got.bands:
        assert band.x1 > band.x0, f"leg {band.index} runs backwards"


def test_a_bus_sits_past_everything_that_reaches_it():
    """Longest path, not shortest: the destination here is reached directly
    *and* through crvUSD, so it has to sit past both."""
    depth = layers(split())
    assert depth[0] == 0
    assert depth[1] == 1
    assert depth[2] == 2, "reached directly at layer 1 and via crvUSD at 2"


def test_the_columns_do_not_depend_on_the_order_the_legs_arrive_in():
    """A leg listed before the leg that feeds it must not pull its bus left.

    The router orders legs so a node's inflows precede its outflows -- it has
    to, to execute them -- but relying on that put a token in the column
    *before* the token it is made from the moment the order differed.
    """
    forward = FakeDiagram(
        buses=[FakeBus(0, "A", is_source=True), FakeBus(1, "B"),
               FakeBus(2, "C", is_dest=True)],
        elements=[FakeElement(0, 0, 1, 100.0, target="0xa"),
                  FakeElement(1, 1, 2, 100.0, target="0xb")],
        order=[0, 1, 2],
    )
    backward = FakeDiagram(
        buses=list(forward.buses),
        # The same route, the second leg listed first.
        elements=[FakeElement(0, 1, 2, 100.0, target="0xb"),
                  FakeElement(1, 0, 1, 100.0, target="0xa")],
        order=[0, 1, 2],
    )
    assert layers(forward) == layers(backward) == {0: 0, 1: 1, 2: 2}
    for diagram in (forward, backward):
        for band in layout(diagram, 400, 300).bands:
            assert band.x1 > band.x0


def test_a_route_that_revisits_a_token_still_fits_its_frame():
    """The bus graph is not always acyclic -- a route can sell a token it
    bought back, which the router reports as a pool used more than once.

    Relaxing over such a loop pushed every bus rightwards until it hit the
    iteration bound, and the whole diagram ended up crushed into the last
    fifth of the paper.
    """
    diagram = FakeDiagram(
        buses=[FakeBus(0, "A", is_source=True), FakeBus(1, "B"),
               FakeBus(2, "C"), FakeBus(3, "D", is_dest=True)],
        elements=[
            FakeElement(0, 0, 1, 100.0, target="0xa"),
            FakeElement(1, 1, 2, 100.0, target="0xb"),
            FakeElement(2, 2, 1, 50.0, target="0xc"),   # back to B
            FakeElement(3, 2, 3, 50.0, target="0xd"),
        ],
        order=[0, 1, 2, 3],
    )
    depth = layers(diagram)
    assert max(depth.values()) <= 3, f"a loop must not inflate the columns: {depth}"
    got = layout(diagram, 400, 300)
    assert min(bus.x for bus in got.buses) == 0, "the picture starts at the edge"
    assert max(bus.x + bus.width for bus in got.buses) == 400


def test_a_single_leg_fills_the_height():
    got = layout(straight(), 400, 200)
    assert len(got.bands) == 1
    band = got.bands[0]
    assert band.height > 100, "one leg carries everything"
    assert band.x0 < band.x1


def test_ribbons_leaving_one_bus_are_contiguous_and_fill_it():
    """A Sankey's links meet at a node with no gap between them; the gaps in
    this picture are between *buses*."""
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    out = sorted((b for b in got.bands if b.x0 == source.x + source.width),
                 key=lambda b: b.y0)
    assert len(out) == 2
    first, second = out
    assert second.y0 == first.y0 + first.height, "no gap, and no overlap"
    assert abs((second.y0 + second.height) - (source.y + source.height)) < 0.01


def test_ribbons_arriving_at_one_bus_do_not_overlap_either():
    got = layout(split(), 400, 300)
    dest = next(bus for bus in got.buses if bus.is_dest)
    into = sorted((b for b in got.bands if b.x1 == dest.x), key=lambda b: b.y1)
    assert len(into) == 2
    first, second = into
    assert second.y1 >= first.y1 + first.height - 0.01


def test_a_split_is_drawn_in_proportion():
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    sixty, forty = sorted(
        (b for b in got.bands if b.x0 == source.x + source.width),
        key=lambda b: -b.share,
    )
    assert sixty.share == 60.0 and forty.share == 40.0
    assert abs(sixty.height / forty.height - 1.5) < 0.05, "60/40, near enough"


def test_a_leg_is_drawn_by_its_share_of_the_route_not_of_its_node():
    """The last leg of a split carries 100% *of its node*, which is 60% of the
    trade. Drawn as it comes it would look like the whole thing."""
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    big = next(b for b in got.bands
               if b.x0 == source.x + source.width and b.share == 60.0)
    onward = next(b for b in got.bands if b.share == 100.0)
    assert abs(onward.height - big.height) < 0.01


def test_a_leg_carrying_almost_nothing_is_still_visible():
    diagram = FakeDiagram(
        buses=[FakeBus(0, "A", is_source=True), FakeBus(1, "B", is_dest=True)],
        elements=[FakeElement(0, 0, 1, 99.6, "big", "0xaaa"),
                  FakeElement(1, 0, 1, 0.4, "tiny", "0xbbb")],
        order=[0, 1],
    )
    got = layout(diagram, 400, 200)
    assert min(band.height for band in got.bands) >= MIN_BAND


def test_two_buses_in_one_column_are_stacked_with_room_for_their_names():
    diagram = FakeDiagram(
        buses=[FakeBus(0, "A", is_source=True), FakeBus(1, "B"),
               FakeBus(2, "C"), FakeBus(3, "D", is_dest=True)],
        elements=[
            FakeElement(0, 0, 1, 50.0, target="0xa"),
            FakeElement(1, 0, 2, 50.0, target="0xb"),
            FakeElement(2, 1, 3, 100.0, target="0xc"),
            FakeElement(3, 2, 3, 100.0, target="0xd"),
        ],
        order=[0, 1, 2, 3],
    )
    got = layout(diagram, 400, 400)
    middle = sorted((bus for bus in got.buses if bus.layer == 1),
                    key=lambda b: b.y)
    assert len(middle) == 2
    upper, lower = middle
    assert lower.y - (upper.y + upper.height) == NODE_GAP


def test_the_whole_thing_stays_inside_the_frame():
    got = layout(split(), 400, 300)
    for bus in got.buses:
        assert bus.y >= 0
        assert bus.y + bus.height <= 300
    for band in got.bands:
        assert min(band.y0, band.y1) >= 0
        assert max(band.y0, band.y1) + band.height <= 300


def test_summarise_counts_pools_not_legs():
    """A route through one pool twice is two legs through one market."""
    diagram = FakeDiagram(elements=[
        FakeElement(0, 0, 1, 100.0, target="0xAAA"),
        FakeElement(1, 1, 2, 100.0, target="0xaaa"),
        FakeElement(2, 2, 3, 100.0, target="0xbbb"),
    ])
    assert summarise(diagram) == (2, 3)
