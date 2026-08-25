"""A pool's readings, turned into the curve its liquidity sits on.

`curve.liquidity` does the geometry and knows nothing about where a pool's
numbers came from.  This is the join: what `PoolContract.parameters()` and
`reserves()` bring back, matched to the family that reads them.

**Which family, decided rather than guessed.**  Curve's API type is not enough
and neither are the coins: a YieldBasis pool and the cryptoswap next door sit
in the same factory holding crvUSD on one side, and one of them runs the
stableswap invariant pegged to `price_scale` while the other does not.  So the
candidates are built and the one that reproduces the pool's own marginal price
is kept.  That is the same test `tools/liquidity_survey.py` applies against a
live chain, and it settles stableswap, FX swap, cryptoswap and tricrypto with
one rule.

Flet-free, so the page can ask for a profile and draw whatever comes back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import liquidity
from .liquidity import Curve, DepthError

#: What `A()` is missing beside `A_precise()`: the stable families carry two
#: digits of amplification that the plain getter rounds away.
A_PRECISION = 100

#: `A_MULTIPLIER` on the crypto families, which is also the precision an FX
#: Swap runs its stableswap invariant at.
A_MULTIPLIER = 10_000

#: How far a candidate's marginal price may sit from the pool's own before it
#: is rejected.  Generous by the standards of what fits -- the right family
#: lands inside 0.02% and the wrong one is out by whole percent -- because the
#: comparison carries the pool's fee, read separately and rounded.
FIT_TOLERANCE = 0.01


@dataclass(frozen=True)
class Reading:
    """One pool, as much of it as the chart needs.

    `quoted` is what the pool itself says a marginal trade of the pair costs,
    with its fee taken back out.  Without it the family cannot be settled, so
    a reading that has none can only be drawn if the pool is unambiguous.
    """

    balances: tuple[int, ...]
    decimals: tuple[int, ...]
    values: dict[str, int]
    rates: tuple[int, ...] = ()

    @property
    def coins(self) -> int:
        return len(self.balances)

    def get(self, *names: str) -> int | None:
        for name in names:
            found = self.values.get(name)
            if found:
                return found
        return None


@dataclass(frozen=True)
class Fitted:
    """A curve, and what turned out to be holding it up."""

    curve: Curve
    family: str
    seed: float


def _stableswap(reading: Reading) -> list[tuple[str, Curve, float]]:
    amp = reading.get("A_precise")
    if amp is None:
        plain = reading.get("A")
        if plain is None:
            return []
        amp = plain * A_PRECISION
    rates = reading.rates
    if len(rates) != reading.coins or any(rate <= 0 for rate in rates):
        # A legacy pool's `RATES` are its decimals and nothing else; an ng
        # pool folds an LST's exchange rate in beside them, which is why the
        # read is preferred wherever it answered.
        rates = tuple(10 ** (36 - d) for d in reading.decimals)
    built = liquidity.stableswap_curve(
        reading.balances, rates, amp, reading.decimals)
    return [("stableswap", built, liquidity.stableswap_seed(amp))]


def _crypto(reading: Reading) -> list[tuple[str, Curve, float]]:
    amp = reading.get("A")
    gamma = reading.get("gamma")
    if amp is None or gamma is None:
        return []
    precisions = [10 ** (18 - d) for d in reading.decimals]
    seed = liquidity.crypto_seed(gamma, amp, n=reading.coins)
    out: list[tuple[str, Curve, float]] = []
    if reading.coins == 3:
        scale = (reading.get("price_scale") or 0,
                 reading.get("price_scale_1") or 0)
        if not all(scale):
            return []
        xp = _crypto_xp(reading, precisions, scale)
        for legacy in (False, True):
            multiplier = 100 if legacy else A_MULTIPLIER
            invariant = reading.get("D") or _solved(xp, amp, gamma, multiplier)
            if not invariant:
                continue
            with_error(out, f"tricrypto{' legacy' if legacy else ''}", seed,
                       lambda inv=invariant, m=multiplier, lg=legacy:
                       liquidity.tricrypto_curve(
                           reading.balances, precisions, scale, inv, amp,
                           gamma, legacy=lg, a_multiplier=m))
        return out
    pegged = reading.get("price_scale") or 0
    if not pegged:
        return []
    xp = _crypto_xp(reading, precisions, (pegged,))
    for family, shape in (("twocrypto", {"stable": False}),
                          ("fx swap", {"stable": True}),
                          ("twocrypto legacy",
                           {"stable": False, "legacy_pool": True})):
        invariant = reading.get("D") or _solved(
            xp, amp, gamma, A_MULTIPLIER, stable=shape["stable"])
        if not invariant:
            continue
        with_error(out, family, seed,
                   lambda inv=invariant, kind=shape: liquidity.twocrypto_curve(
                       reading.balances, precisions, pegged, inv, amp, gamma,
                       **kind))
    return out


def _crypto_xp(reading: Reading, precisions: Sequence[int],
               scale: Sequence[int]) -> list[float]:
    """The balances in the space the crypto families solve in."""
    xp = [float(reading.balances[0] * precisions[0])]
    for k in range(1, reading.coins):
        xp.append(float(reading.balances[k] * precisions[k] * scale[k - 1]
                        // 10**18))
    return xp


def _solved(xp: Sequence[float], amp: int, gamma: int, multiplier: int, *,
            stable: bool = False) -> int:
    """`D` where the pool does not publish one. See `crypto_invariant`."""
    try:
        return int(liquidity.crypto_invariant(
            xp, amp / multiplier, gamma / 1e18, stable=stable,
            a_multiplier=float(multiplier)))
    except DepthError:
        return 0


def with_error(out: list, family: str, seed: float, build) -> None:
    """Add a candidate, skipping the ones this pool's numbers will not build."""
    try:
        out.append((family, build(), seed))
    except (DepthError, ArithmeticError, ValueError):
        return


def candidates(reading: Reading) -> list[tuple[str, Curve, float]]:
    """Every curve this pool might be on."""
    if reading.coins < 2 or any(b <= 0 for b in reading.balances):
        return []
    if reading.get("gamma") is None:
        return _stableswap(reading)
    return _crypto(reading)


def fit(reading: Reading, i: int, j: int, quoted: float | None = None
        ) -> Fitted:
    """The curve whose marginal price matches what the pool quotes.

    With no quote to check against, the first candidate is taken -- right for
    a stableswap, where there is only one, and a guess for a crypto pool.  The
    caller is better off passing one: a small `get_dy` is a single call and it
    is the difference between drawing this pool's curve and drawing a curve.
    """
    built = candidates(reading)
    if not built:
        raise DepthError("this pool's numbers do not describe a curve")
    if quoted is None or quoted <= 0:
        family, curve, seed = built[0]
        return Fitted(curve=curve, family=family, seed=seed)
    best: tuple[float, str, Curve, float] | None = None
    for family, curve, seed in built:
        try:
            here = liquidity.spot_price(curve, i, j)
        except (DepthError, ArithmeticError):
            continue
        error = abs(here - quoted) / quoted
        if best is None or error < best[0]:
            best = (error, family, curve, seed)
    if best is None:
        raise DepthError("no candidate curve would price this pair")
    error, family, curve, seed = best
    if error > FIT_TOLERANCE:
        raise DepthError(
            f"the closest curve misprices this pair by {error * 100:.2f}%")
    return Fitted(curve=curve, family=family, seed=seed)


def profile(reading: Reading, i: int, j: int, *, quoted: float | None = None,
            low: float = 0.0, high: float = 0.0, points: int = 160):
    """A depth profile for one pair, framed on the pool's own feature."""
    fitted = fit(reading, i, j, quoted)
    if not (low and high):
        low, high = liquidity.auto_window(fitted.curve, i, j, seed=fitted.seed)
    return liquidity.profile(fitted.curve, i, j, low=low, high=high,
                             points=points), fitted


__all__ = ["A_MULTIPLIER", "A_PRECISION", "FIT_TOLERANCE", "Fitted", "Reading",
           "candidates", "fit", "profile"]
