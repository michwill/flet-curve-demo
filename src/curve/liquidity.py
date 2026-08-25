"""How much a pool's liquidity is worth at each price, along its own curve.

Uniswap v3 concentrates liquidity into ticks, so its depth chart is a bar per
tick and the bars are what the positions actually are.  Curve's invariants are
smooth: the liquidity is spread continuously over every price the curve can
reach, and the shape of that spread *is* the curve's curvature.  A stableswap
with A=4000 puts almost all of it within a few tenths of a percent of the peg;
a cryptoswap spreads it over decades either side of `price_scale`.

**What is plotted.**  For a pair `(i, j)`, hold every other balance fixed and
follow the invariant: `j`'s balance is a function of `i`'s, and the marginal
price is the slope of that function.  The depth at a price `p` is how much of
coin `i` has to move for the price to travel a 1% band around `p`.  That is
`|dx / d ln p| / 100`, and since `p = -dy/dx`, `dx/dlnp` is `p` over the
curve's second derivative -- liquidity density is the inverse of the curvature,
which is the whole reason a flat curve is a deep one.

**Measured, not differentiated.**  A second derivative taken numerically off a
Newton solver is noise, so nothing here differentiates twice.  The price axis
is gridded at a fixed multiplicative step, the balance at each grid price is
found by bisection, and the depth is the difference between neighbours scaled
to 1%.  That is the definition rather than an approximation of it, and every
value comes from one monotone solve.

The solvers are the router's own -- the same arithmetic that has to agree with
the chain to the wei for a quote to be admitted -- so the curve traced here is
the pool's, not a model of it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: The price grid's multiplicative step.  Finer than the 1% the depth is
#: quoted in, so the line is smooth rather than a staircase, and the depth is
#: scaled up to the 1% band it names.
STEP = 0.001

#: Where the depth is quoted, as a multiple of the grid step.
BAND = 0.01

#: How far the bisection is allowed to hunt for a bracketing balance, as a
#: multiple of the current one.  A cryptoswap price runs away from the scale
#: fast, so this is generous; the loop stops as soon as the price is bracketed.
MAX_REACH = 1e9

#: Relative width the bisection closes to.  A price is a ratio of two Newton
#: solves, so there is no sense chasing it below their own tolerance.
TOLERANCE = 1e-9
MAX_STEPS = 200


class DepthError(ValueError):
    """The curve cannot be followed here."""


@dataclass(frozen=True)
class Curve:
    """One pool's invariant, reduced to what a depth profile needs.

    `xp` is the pool's state in whatever common space its family solves in:
    rate-scaled balances for stableswap, `price_scale`-adjusted ones for the
    crypto families.  `solve(xp, j)` returns the `j` entry that restores the
    invariant for the rest of `xp`, which is the one thing every family has.

    `scale[k]` takes an `xp` value to whole tokens of coin `k`.  It is a plain
    multiplier in every family -- that is what lets the price come out of the
    `xp`-space slope with one ratio.
    """

    xp: tuple[float, ...]
    solve: Callable[[list[float], int, int], float]
    scale: tuple[float, ...]

    def y_at(self, i: int, j: int, x: float) -> float:
        """`j`'s balance when `i` holds `x`, everything else untouched."""
        state = list(self.xp)
        state[i] = x
        return self.solve(state, i, j)

    def price_at(self, i: int, j: int, x: float) -> float:
        """The marginal price of `i` in units of `j`, in token terms.

        A central difference rather than a closed form: three families would
        need three of those, each a place for the chain and this to drift
        apart, and the slope of a monotone solve is well behaved.  The step is
        relative, so it holds up wherever on the curve this is asked.
        """
        h = abs(x) * 1e-7
        if h <= 0.0:
            raise DepthError("no balance to differentiate at")
        low = self.y_at(i, j, x - h)
        high = self.y_at(i, j, x + h)
        slope = -(high - low) / (2 * h)
        if not math.isfinite(slope) or slope <= 0.0:
            raise DepthError("the curve does not fall here")
        return slope * self.scale[j] / self.scale[i]


@dataclass(frozen=True)
class Sample:
    """One point of the profile."""

    price: float
    #: Coin `i` per 1% of price range, in whole tokens.
    depth: float


@dataclass(frozen=True)
class Profile:
    """A pool's depth across a price window, and where it is trading now."""

    samples: tuple[Sample, ...]
    spot: float
    #: Which pair this followed, as indices into the pool's coins.
    pair: tuple[int, int]

    def __bool__(self) -> bool:
        return bool(self.samples)

    @property
    def peak(self) -> float:
        return max((s.depth for s in self.samples), default=0.0)


def spot_price(curve: Curve, i: int, j: int) -> float:
    """What the pool trades `i` for `j` at right now, before fees."""
    return curve.price_at(i, j, curve.xp[i])


def _walk(price_of, start: float, target: float, *, grow: bool
          ) -> tuple[float, float]:
    """Step geometrically until `target` sits between two balances.

    Easing off rather than giving up is the whole of it.  A doubling step
    overshoots the end of the curve long before it overshoots the price, and
    out there the invariant simply stops solving -- so a probe that fails is
    not "no such price", it is "not that far in one step".  Halving what is
    left of the step walks up to the edge instead of falling off it.
    """
    step = 2.0
    x = start
    for _ in range(MAX_STEPS):
        trial = x * step if grow else x / step
        if trial > start * MAX_REACH or trial < start / MAX_REACH:
            raise DepthError("the price does not reach there")
        found = price_of(trial)
        if found is None:
            step = 1.0 + (step - 1.0) * 0.5
            if step < 1.0 + 1e-9:
                raise DepthError("the curve ends before that price")
            continue
        if (found <= target) if grow else (found >= target):
            return (x, trial) if grow else (trial, x)
        x = trial
    raise DepthError("the price does not reach there")


def balance_at_price(curve: Curve, i: int, j: int, target: float) -> float:
    """The `i` balance whose marginal price is `target`.

    Bisection, because price falls monotonically as `i`'s balance grows -- more
    of a coin in the pool is a cheaper coin, on every invariant here.  That
    monotonicity is what makes a bracket enough and stops this needing a
    derivative it would have to trust.
    """
    if target <= 0.0:
        raise DepthError("a price has to be positive")
    here = curve.xp[i]

    def price_of(x: float) -> float | None:
        try:
            return curve.price_at(i, j, x)
        except DepthError:
            return None

    price_here = price_of(here)
    if price_here is None:
        raise DepthError("the pool is not on its own curve")
    if target < price_here:
        low, high = _walk(price_of, here, target, grow=True)
    else:
        low, high = _walk(price_of, here, target, grow=False)
    for _ in range(MAX_STEPS):
        middle = 0.5 * (low + high)
        found = price_of(middle)
        if found is None:
            high = middle
        elif found > target:
            low = middle
        else:
            high = middle
        if high - low <= abs(high) * TOLERANCE:
            break
    return 0.5 * (low + high)


#: Where a window's edge sits, as a share of the depth at spot.  An eighth
#: shows the peak with its shoulders either side and stops well before the
#: tails, which run for decades and say nothing.
EDGE_SHARE = 0.125

#: How far the window is allowed to open, as a relative price offset.  A
#: constant-product curve never reaches `EDGE_SHARE` anywhere useful -- its
#: depth goes as `p**-0.5`, so an eighth is 64x the price -- and a chart of
#: that is a flat line with the pool somewhere in the middle.  Three times
#: the price either way is where a cryptoswap's own shape is still legible.
MAX_WIDTH = 2.0

#: And how far it must open regardless, so a very flat pool still shows the
#: curve either side of its peak rather than one column of pixels.
MIN_WIDTH = 2e-4

#: The seed for a stableswap's feature width.  Measured against three mainnet
#: pools: the depth halves at 1.31/A, 0.87/A and 0.96/A, so `1/A` is the scale
#: and the search below only has to find the constant.
#:
#: There is no such seed for the crypto families and it is not for want of
#: algebra.  Their width is not a property of `A` and `gamma` alone -- it is
#: where the *pair* sits relative to the amplified region, and the three pairs
#: of one tricrypto pool measured 7.2e-3, 0.10 and 2.52.  The last of those is
#: the constant-product answer arriving on schedule: away from the peg
#: `x = sqrt(k/p)`, so the depth goes as `p**-0.5` and halves when the price
#: quadruples.  So crypto pairs start from a plain guess and are measured.
CRYPTO_SEED = 0.05


def stableswap_seed(amp: int, *, a_precision: int = 100) -> float:
    """The scale of a stableswap's peak, as a relative price offset."""
    a = amp / a_precision
    return 1.0 / a if a > 0 else CRYPTO_SEED


def depth_at(curve: Curve, i: int, j: int, price: float, *,
             band: float = 1e-4) -> float:
    """Coin `i` per 1% of price range at `price`, in `xp` units."""
    below = balance_at_price(curve, i, j, price / (1 + band))
    above = balance_at_price(curve, i, j, price * (1 + band))
    return abs(above - below) * (BAND / (2 * math.log1p(band)))


def auto_window(curve: Curve, i: int, j: int, *, seed: float = CRYPTO_SEED,
                floor: float = MIN_WIDTH, ceiling: float = MAX_WIDTH
                ) -> tuple[float, float]:
    """A price window that frames the pool's own feature, whatever its size.

    Sampling a stableswap over +/-10% draws a spike one pixel wide with a flat
    line either side; sampling a cryptoswap over +/-0.1% draws a straight line
    and no feature at all.  The two differ by four orders of magnitude, so the
    window has to come from the curve rather than from a constant.

    Found rather than derived: the offset is grown until the depth there has
    fallen to `EDGE_SHARE` of the depth at spot.  `seed` only says where to
    start looking, which is what the analytic width is good for.
    """
    spot = spot_price(curve, i, j)
    here = depth_at(curve, i, j, spot)
    if here <= 0.0:
        raise DepthError("no depth at the current price")
    low, high = max(floor, seed * 1e-3), min(ceiling, max(seed * 1e3, floor * 10))
    for _ in range(48):
        middle = math.sqrt(low * high)
        try:
            there = depth_at(curve, i, j, spot * (1 + middle))
        except DepthError:
            high = middle
            continue
        if there > here * EDGE_SHARE:
            low = middle
        else:
            high = middle
        if high / low < 1.02:
            break
    width = min(max(math.sqrt(low * high), floor), ceiling)
    return spot / (1 + width), spot * (1 + width)


def profile(curve: Curve, i: int, j: int, *, low: float, high: float,
            points: int = 240) -> Profile:
    """Depth across `[low, high]`, in coin `i` per 1% of price range.

    The grid is geometric because the quantity is: a 1% band is 1% wherever it
    sits, so equal ratios and not equal differences are what make the line
    comparable across the width of the chart.
    """
    if not (0.0 < low < high):
        raise DepthError("the window has to be a positive price range")
    spot = spot_price(curve, i, j)
    # A grid step of its own where the window is narrow, so a zoomed-in chart
    # gets a finer band rather than the same one stretched.
    # `points` is what it says: the depth is normalised to a 1% band whatever
    # the step, so a wider window is a coarser grid rather than more of them.
    # Taking the finer of the two made a crypto window 6,000 samples and four
    # seconds, for a line 300 pixels wide.
    span = math.log(high / low)
    count = max(2, points)
    edges: list[tuple[float, float]] = []
    for k in range(count + 1):
        price = low * math.exp(span * k / count)
        try:
            edges.append((price, balance_at_price(curve, i, j, price)))
        except DepthError:
            continue
    samples: list[Sample] = []
    for k in range(len(edges) - 1):
        (p0, x0), (p1, x1) = edges[k], edges[k + 1]
        width = math.log(p1 / p0)
        if width <= 0.0:
            continue
        # Per 1%, whatever the grid step happens to be.
        per_band = abs(x1 - x0) * (BAND / width)
        samples.append(Sample(price=math.sqrt(p0 * p1),
                              depth=per_band * curve.scale[i]))
    return Profile(samples=tuple(samples), spot=spot, pair=(i, j))


# ------------------------------------------------------------------ families


def stableswap_curve(balances: Sequence[int], rates: Sequence[int], amp: int,
                     decimals: Sequence[int], *, a_precision: int = 100) -> Curve:
    """A stableswap pool, from what `parameters()` and `reserves()` return.

    `rates` are `stored_rates()` where the pool has them and `10**(36-dec)`
    where it does not, which is the same thing the router feeds its own model:
    an LST's exchange rate folded in beside the decimals, so `xp` is value
    rather than counts and the curve is the one the pool actually trades on.
    """
    from erouter.core.stableswap import StableSwapError, d_fast, solve_y_fast

    xp = [b * r / 1e18 for b, r in zip(balances, rates, strict=True)]
    if any(v <= 0 for v in xp):
        raise DepthError("a pool with an empty balance has no curve")
    n = len(xp)
    d = d_fast(xp, float(amp), float(a_precision), n)

    def solve(state: list[float], i: int, j: int) -> float:
        # Far enough out the Newton iteration stops converging, which is the
        # end of the curve rather than a fault: the search that walked here
        # has to be able to back off, so it arrives as a `DepthError`.
        try:
            return solve_y_fast(float(amp), float(a_precision), state, d,
                                i, j, state[i])
        except StableSwapError as exc:
            raise DepthError(str(exc)) from exc

    scale = tuple(1e18 / (r * 10 ** dec)
                  for r, dec in zip(rates, decimals, strict=True))
    return Curve(xp=tuple(xp), solve=solve, scale=scale)


def _crypto_scales(price_scale: Sequence[int], n: int) -> tuple[float, ...]:
    """`xp` to whole tokens, for a family that prices everything against 0.

    Coin 0's `xp` is its balance at 1e18 precision whatever its decimals, so
    one scale serves it; every other coin's `xp` has been multiplied by the
    scale that prices it against coin 0, so dividing it back out is what
    returns a count.
    """
    scales = [1e-18]
    for k in range(1, n):
        ps = price_scale[k - 1] / 1e18
        if ps <= 0:
            raise DepthError("a price scale of zero has no curve")
        scales.append(1.0 / (ps * 1e18))
    return tuple(scales)


def twocrypto_curve(balances: Sequence[int], precisions: Sequence[int],
                    price_scale: int, d: int, amp: int, gamma: int, *,
                    stable: bool, v21: bool = True,
                    legacy_pool: bool = False) -> Curve:
    """A twocrypto-ng pool: cryptoswap, or the FX Swap's stableswap backend.

    `stable` is the pool's own `MATH()`, never a guess from its coins -- an FX
    Swap solves the stableswap invariant in `price_scale`-adjusted space, which
    is a different curve from the cryptoswap one at the same parameters.
    """
    from erouter.core.twocrypto import Twocrypto, TwocryptoError

    model = Twocrypto(
        balances=(balances[0], balances[1]),
        precisions=(precisions[0], precisions[1]),
        price_scale=price_scale, d=d, amp=amp, gamma=gamma,
        mid_fee=0, out_fee=0, fee_gamma=0,
        stable=stable, v21=v21, legacy_pool=legacy_pool,
    )
    xp = [balances[0] * precisions[0],
          balances[1] * precisions[1] * price_scale // 10**18]
    if any(v <= 0 for v in xp):
        raise DepthError("a pool with an empty balance has no curve")

    def solve(state: list[float], _i: int, j: int) -> float:
        try:
            return float(model._y_fast([int(v) for v in state], j))
        except (TwocryptoError, ArithmeticError) as exc:
            raise DepthError(str(exc)) from exc

    return Curve(xp=tuple(float(v) for v in xp), solve=solve,
                 scale=_crypto_scales([price_scale], 2))


def tricrypto_curve(balances: Sequence[int], precisions: Sequence[int],
                    price_scale: Sequence[int], d: int, amp: int, gamma: int,
                    *, legacy: bool = False,
                    a_multiplier: int = 10_000) -> Curve:
    """A tricrypto pool, whose pairs are every ordered two of its three coins."""
    from erouter.core.tricrypto import PRECISION, TricryptoError, newton_y_fast

    xp = [balances[0] * precisions[0]]
    for k in (1, 2):
        xp.append(balances[k] * precisions[k] * price_scale[k - 1] // 10**18)
    if any(v <= 0 for v in xp):
        raise DepthError("a pool with an empty balance has no curve")
    # `newton_y_fast` works in whole units rather than wei, which is also what
    # keeps a three-coin Newton solve away from the ends of a double.
    a = amp / a_multiplier
    g = gamma / PRECISION
    dd = d / PRECISION

    def solve(state: list[float], _i: int, j: int) -> float:
        try:
            return newton_y_fast(a, g, [v / PRECISION for v in state], dd, j,
                                 legacy) * PRECISION
        except TricryptoError as exc:
            raise DepthError(str(exc)) from exc

    return Curve(xp=tuple(float(v) for v in xp), solve=solve,
                 scale=_crypto_scales(price_scale, 3))


__all__ = ["BAND", "Curve", "DepthError", "Profile", "Sample",
           "balance_at_price", "profile", "spot_price", "stableswap_curve",
           "tricrypto_curve", "twocrypto_curve"]
