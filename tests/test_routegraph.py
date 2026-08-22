"""The route diagram's geometry, which is where its mistakes are.

A band that overlaps its neighbour, a column off the edge, shares that do not
add up -- none of that needs a window to find, which is why the arithmetic is
a module of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ui.routegraph import BAND_GAP, MIN_BAND, layout, summarise


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
    right = max(bus.x + bus.width for bus in got.buses)
    assert right == 400, "the last column ends at the edge, not past it"


def test_a_single_leg_fills_the_height():
    got = layout(straight(), 400, 200)
    assert len(got.bands) == 1
    band = got.bands[0]
    assert band.height > 100, "one leg carries everything"
    assert band.x0 < band.x1, "and runs left to right"


def test_bands_out_of_one_bus_do_not_overlap():
    got = layout(split(), 400, 300)
    out_of_source = sorted((b for b in got.bands if b.x0 == got.buses[0].width),
                           key=lambda b: b.y0)
    assert len(out_of_source) == 2
    first, second = out_of_source
    assert second.y0 >= first.y0 + first.height, "the second starts below the first"
    assert second.y0 - (first.y0 + first.height) == BAND_GAP


def test_a_split_is_drawn_in_proportion():
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    sixty, forty = sorted(
        (b for b in got.bands if b.x0 == source.x + source.width),
        key=lambda b: -b.share,
    )
    assert sixty.share == 60.0 and forty.share == 40.0
    assert sixty.height > forty.height
    assert abs(sixty.height / forty.height - 1.5) < 0.05, "60/40, near enough"


def test_a_leg_is_drawn_by_its_share_of_the_route_not_of_its_node():
    """The last leg of a split carries 100% *of its node*, which is 60% of the
    trade. Drawn as it comes it would look like the whole thing."""
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    big = next(b for b in got.bands
               if b.x0 == source.x + source.width and b.share == 60.0)
    onward = next(b for b in got.bands if b.share == 100.0)
    assert abs(onward.height - big.height) < 1.0, (
        "the leg carrying that 60% onward is the same width as the 60%"
    )


def test_a_leg_carrying_almost_nothing_is_still_visible():
    """Below a couple of pixels a band cannot be told from the gap above it."""
    diagram = FakeDiagram(
        buses=[FakeBus(0, "A", is_source=True), FakeBus(1, "B", is_dest=True)],
        elements=[FakeElement(0, 0, 1, 99.6, "big", "0xaaa"),
                  FakeElement(1, 0, 1, 0.4, "tiny", "0xbbb")],
        order=[0, 1],
    )
    got = layout(diagram, 400, 200)
    assert min(band.height for band in got.bands) >= MIN_BAND


def test_the_shares_out_of_a_node_fill_its_bus():
    """A node's outgoing bands together are as tall as the node."""
    got = layout(split(), 400, 300)
    source = next(bus for bus in got.buses if bus.is_source)
    bands = [b for b in got.bands if b.x0 == source.x + source.width]
    total = sum(b.height for b in bands) + BAND_GAP * (len(bands) - 1)
    assert abs(total - source.height) < 1.0


def test_summarise_counts_pools_not_legs():
    """A route through one pool twice is two legs through one market."""
    diagram = FakeDiagram(elements=[
        FakeElement(0, 0, 1, 100.0, target="0xAAA"),
        FakeElement(1, 1, 2, 100.0, target="0xaaa"),
        FakeElement(2, 2, 3, 100.0, target="0xbbb"),
    ])
    assert summarise(diagram) == (2, 3)
