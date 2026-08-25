"""Liquidity density along a pool's own bonding curve.

Synthetic pools throughout: the arithmetic is the router's, which has its own
differential tests against the chain, and what wants checking here is the
geometry laid over it -- that the peak lands where the curve is flat, that the
window finds a feature four orders of magnitude wide either way, and that the
depth integrates back to the balance it was differenced from.
"""

from __future__ import annotations

import math

import pytest

from curve import liquidity as L

#: 1e18 in the common `xp` space, which is what a stableswap balance is scaled
#: to whatever the coin's own decimals.
ONE = 10**18


def stable(balances, amp=100, decimals=(18, 18)):
    """A stableswap over whole-token balances, at 18 decimals throughout."""
    return L.stableswap_curve(
        [int(b * ONE) for b in balances],
        [10 ** (36 - d) for d in decimals],
        amp * 100,
        decimals,
    )


def test_a_balanced_pool_trades_at_one():
    curve = stable([1_000_000, 1_000_000])
    assert L.spot_price(curve, 0, 1) == pytest.approx(1.0, rel=1e-9)


def test_more_of_a_coin_makes_it_cheaper():
    """The monotonicity the bisection rests on, stated as a test.

    Every invariant here falls the same way, and if one did not the bracket
    search would converge on nothing in particular rather than fail.
    """
    curve = stable([1_000_000, 1_000_000])
    here = curve.xp[0]
    prices = [curve.price_at(0, 1, here * m) for m in (0.6, 0.8, 1.0, 1.2, 1.4)]
    assert prices == sorted(prices, reverse=True)


def test_the_balance_at_a_price_is_the_price_at_that_balance():
    """`balance_at_price` inverts `price_at`, which is all it claims to do."""
    curve = stable([1_000_000, 1_000_000])
    for target in (0.999, 0.9999, 1.0001, 1.001):
        x = L.balance_at_price(curve, 0, 1, target)
        assert curve.price_at(0, 1, x) == pytest.approx(target, rel=1e-6)


def test_the_peak_sits_where_the_curve_is_flattest():
    """A balanced stableswap is deepest at its peg, which is the whole point
    of the shape: the flat part of the curve is the liquid part."""
    curve = stable([1_000_000, 1_000_000], amp=200)
    low, high = L.auto_window(curve, 0, 1, seed=L.stableswap_seed(200 * 100))
    got = L.profile(curve, 0, 1, low=low, high=high, points=80)
    best = max(got.samples, key=lambda s: s.depth)
    # Within a sample of the balance point, which is where `peak_price` says
    # the curve is flattest.  The window carries four peak widths of air
    # either side now, so a sample is wider than it used to be.
    step = math.log(high / low) / len(got.samples)
    assert abs(math.log(best.price / L.peak_price(curve, 0, 1))) < step
    assert got.samples[0].depth < best.depth
    assert got.samples[-1].depth < best.depth


def test_depth_integrates_back_to_the_balance_it_came_from():
    """The identity behind the units: summing `depth * dlnp` over the window
    returns the balance travelled, because the depth *is* `dx/dlnp` scaled to
    a 1% band.  A definition that did not close here would be a different
    quantity wearing the label.
    """
    curve = stable([1_000_000, 1_000_000], amp=100)
    low, high = 0.99, 1.01
    got = L.profile(curve, 0, 1, low=low, high=high, points=400)
    total = 0.0
    for k in range(len(got.samples) - 1):
        first, second = got.samples[k], got.samples[k + 1]
        width = math.log(second.price / first.price)
        total += 0.5 * (first.depth + second.depth) * width / L.BAND
    travelled = abs(L.balance_at_price(curve, 0, 1, high)
                    - L.balance_at_price(curve, 0, 1, low)) * curve.scale[0]
    assert total == pytest.approx(travelled, rel=2e-3)


def test_a_flatter_pool_has_a_narrower_feature():
    """`1/A` is the scale of a stableswap's peak -- measured at 1.31/A, 0.87/A
    and 0.96/A on three mainnet pools -- so ten times the A is about a tenth
    of the window.  The constant is what `auto_window` goes and finds; this
    pins the scaling it is finding it around.
    """
    def width(amp):
        curve = stable([1_000_000, 1_000_000], amp=amp)
        _low, high = L.auto_window(curve, 0, 1,
                                   seed=L.stableswap_seed(amp * 100))
        return high / L.spot_price(curve, 0, 1) - 1

    tight, loose = width(1000), width(100)
    assert tight < loose
    assert 5 < loose / tight < 20, "an order of A is an order of width"


def test_the_window_never_opens_wider_than_the_cap():
    """A constant-product curve's depth goes as `p**-0.5`, so the edge share is
    64x the price away and the search would run to the horizon."""
    curve = stable([1_000_000, 1_000_000], amp=1)
    low, high = L.auto_window(curve, 0, 1)
    spot = L.spot_price(curve, 0, 1)
    assert high / spot - 1 <= L.MAX_WIDTH + 1e-9
    assert low < spot < high


def test_the_window_never_closes_below_the_floor():
    curve = stable([1_000_000, 1_000_000], amp=100_000)
    _low, high = L.auto_window(curve, 0, 1,
                               seed=L.stableswap_seed(100_000 * 100))
    spot = L.spot_price(curve, 0, 1)
    assert high / spot - 1 >= L.MIN_WIDTH - 1e-12


def test_the_grid_is_the_size_it_was_asked_for():
    """`points` caps the samples rather than seeding them: reading it the
    other way made one crypto window 6,000 solves for a 300px line."""
    curve = stable([1_000_000, 1_000_000])
    got = L.profile(curve, 0, 1, low=0.5, high=2.0, points=64)
    assert len(got.samples) <= 64


def test_an_imbalanced_pool_is_deepest_away_from_where_it_trades():
    """Depth is a property of the curve, not of where the pool happens to sit,
    so a lopsided pool trades off its own peak."""
    curve = stable([1_400_000, 600_000], amp=200)
    spot = L.spot_price(curve, 0, 1)
    assert spot < 1.0, "the long side is the cheap side"
    here = L.depth_at(curve, 0, 1, spot)
    at_peg = L.depth_at(curve, 0, 1, 1.0)
    assert at_peg > here


def test_an_empty_balance_has_no_curve():
    with pytest.raises(L.DepthError):
        stable([1_000_000, 0])


def test_a_price_of_zero_is_refused():
    curve = stable([1_000_000, 1_000_000])
    with pytest.raises(L.DepthError):
        L.balance_at_price(curve, 0, 1, 0.0)


def test_a_window_has_to_be_a_range():
    curve = stable([1_000_000, 1_000_000])
    with pytest.raises(L.DepthError):
        L.profile(curve, 0, 1, low=1.0, high=1.0)


def test_the_third_coin_is_held_still():
    """A pair out of three follows the invariant with the rest fixed, which is
    what makes a tricrypto pool six charts rather than one."""
    curve = stable([1_000_000, 1_000_000, 1_000_000], amp=200,
                   decimals=(18, 18, 18))
    # A central difference, so the tolerance is the method's rather than the
    # solver's: the step is relative and the truncation lands around 1e-9.
    assert L.spot_price(curve, 0, 1) == pytest.approx(1.0, rel=1e-7)
    moved = L.balance_at_price(curve, 0, 1, 0.999)
    assert moved > curve.xp[0]


def test_a_stableswap_has_no_background_to_speak_of():
    """The subtraction has to be near enough a no-op where there is nothing
    under the peak, or every stableswap window would come out wrong.

    Not zero in absolute terms -- a stableswap still quotes far from its peg
    -- but small beside the peak, which is what the subtraction cares about.
    A cryptoswap's floor is a different order: measured at a 300th of the peak
    on one Gnosis pool and at four fifths of it on a mainnet pair.
    """
    curve = stable([1_000_000, 1_000_000], amp=200)
    spot = L.spot_price(curve, 0, 1)
    peak = L.depth_at(curve, 0, 1, spot)
    assert L.background(curve, 0, 1) < peak / 100


def test_the_window_measures_the_peak_not_the_background():
    """A curve whose peak is barely above its floor still gets framed on the
    peak.  Measured on the total instead, one mainnet pair at 1.3x background
    ran the search to the horizon and drew its whole peak in one sample.
    """
    curve = stable([1_000_000, 1_000_000], amp=200)
    crest = L.peak_price(curve, 0, 1)
    _low, high = L.auto_window(curve, 0, 1, seed=L.stableswap_seed(200 * 100))
    floor_depth = L.background(curve, 0, 1)
    edge = L.depth_at(curve, 0, 1, high) - floor_depth
    peak = L.depth_at(curve, 0, 1, crest) - floor_depth
    # Well past half height: the window carries `MARGIN_WIDTHS` of the peak's
    # own width beyond where that width was measured.
    assert 0.0 <= edge < peak * L.PEAK_SHARE


def test_the_window_holds_both_the_peak_and_the_price():
    """The two are the same only for a balanced pool.  A window centred on
    spot pushes the peak out of frame exactly when the chart is worth looking
    at -- a pool far enough from balance to be worth asking about.
    """
    curve = stable([1_400_000, 600_000], amp=200)
    spot = L.spot_price(curve, 0, 1)
    crest = L.peak_price(curve, 0, 1)
    assert spot != pytest.approx(crest, rel=1e-6), "an imbalanced pool"
    low, high = L.auto_window(curve, 0, 1, seed=L.stableswap_seed(200 * 100))
    assert low < spot < high
    assert low < crest < high


def test_the_peak_of_a_balanced_pool_is_where_it_trades():
    curve = stable([1_000_000, 1_000_000], amp=200)
    assert L.peak_price(curve, 0, 1) == pytest.approx(
        L.spot_price(curve, 0, 1), rel=1e-6)


def test_the_peak_is_the_balance_point_not_the_spot_price():
    """Solved as `x = y(x)` rather than hunted as a maximum: one monotone root
    instead of a search over a quantity that is two solves deep."""
    curve = stable([1_400_000, 600_000], amp=200)
    crest = L.peak_price(curve, 0, 1)
    at_peak = L.depth_at(curve, 0, 1, crest)
    for offset in (-0.01, -0.001, 0.001, 0.01):
        assert L.depth_at(curve, 0, 1, crest * (1 + offset)) < at_peak


def test_the_crypto_seed_follows_gamma():
    """`gamma` sets the width: the amplified region reaches out to
    `t ~ sqrt(gamma)`, so a hundredth of the gamma is a tenth of the seed."""
    wide = L.crypto_seed(10**16, 400_000)
    narrow = L.crypto_seed(10**14, 400_000)
    assert wide / narrow == pytest.approx(10.0, rel=1e-6)


def test_the_crypto_seed_survives_nonsense():
    assert L.crypto_seed(0, 400_000) == L.CRYPTO_SEED
    assert L.crypto_seed(10**16, 0) == L.CRYPTO_SEED
