"""Check the depth curve against the pool itself, family by family.

`liquidity_survey.py` confirms the *spot* price a model implies.  That is one
point on the curve, and a model can reproduce it while being wrong about the
shape either side -- which is what a depth chart is entirely made of.

So this asks the question the chart asks.  From our curve: how much of coin
`i` has to go in for the marginal price to fall by a given amount?  From the
pool's own model, which is wei-exact against the chain and is what quotes a
route: the same, by moving its balances and re-reading its price.  They are
independent paths to one number and should agree.

    .venv/bin/python tools/depth_check.py [pool[:i:j] ...]

Reads endpoints the way `liquidity_survey.py` does.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from liquidity_survey import (
    endpoints,
    node,
    number,
    shape,
    stored_rates,
    symbol,
)

from curve import depth
from curve import liquidity as L

#: Where along the curve to compare, as shares of the `i` balance.  Fine at
#: the near end: an amplified peak can be a few basis points wide, and a
#: ladder that steps over it says nothing about the width.
LADDER = (1e-7, 3e-7, 1e-6, 3e-6, 1e-5, 3e-5, 1e-4, 3e-4,
          1e-3, 3e-3, 1e-2, 3e-2, 1e-1)

#: The step the chain's marginal price is differenced over, relative to the
#: trade it is taken at.  Large enough that `get_dy` rounding does not show,
#: small enough to still be a slope.
STEP = 0.02

#: The pools the symptom was reported on, and one stableswap for contrast.
DEFAULT = [
    ("ethereum", "0x4eBdF703948ddCEA3B11f675B4D1Fba9d2414A14", None),  # TriCRV
    ("ethereum", "0xB576491F1E6e5E62f1d8F26062Ee822B40B0E0d4", None),  # CVX/ETH
    ("ethereum", "0x4dece678ceceb27446b35c672dc7d61f30bad69e", None),  # USDC/crvUSD
    ("gnosis", "0x056C6C5e684CeC248635eD86033378Cc444459B0", None),    # EURe/x3CRV
    # tricrypto2, whose pairs read very different widths: the pair holding the
    # surplus coin is the wide one.
    ("ethereum", "0xD51a44d3FaE010294C616388b506AcdA1bfAAE46", (2, 0)),
    ("ethereum", "0xD51a44d3FaE010294C616388b506AcdA1bfAAE46", (2, 1)),
]


def reading_of(call, pool: str, decimals, balances) -> depth.Reading:
    """What `PoolContract.curve_state` would hand the chart."""
    values: dict[str, int] = {}
    for key in ("A", "A_precise", "gamma", "D", "fee", "mid_fee", "out_fee",
                "fee_gamma", "offpeg_fee_multiplier"):
        got = number(call, pool, f"{key}()")
        if got:
            values[key] = got
    for k, key in enumerate(("price_scale", "price_scale_1")):
        got = number(call, pool, "price_scale(uint256)", k)
        if got:
            values[key] = got
    if "price_scale" not in values:
        got = number(call, pool, "price_scale()")
        if got:
            values["price_scale"] = got
    rates = stored_rates(call, pool, len(balances)) or []
    return depth.Reading(balances=tuple(balances), decimals=tuple(decimals),
                         values=values, rates=tuple(rates))


def chain_dy(call, pool: str, i: int, j: int, dx: int) -> int | None:
    """`get_dy`, whichever spelling this generation answers to."""
    for sig in ("get_dy(uint256,uint256,uint256)",
                "get_dy(int128,int128,uint256)"):
        got = call(pool, sig, i, j, dx)
        if got and got != "0x":
            return int(got, 16)
    return None


def chain_slope(call, pool: str, decimals, i: int, j: int,
                dx: int) -> float | None:
    """The pool's own marginal price once `dx` has gone in.

    Differenced across two `get_dy` calls rather than reconstructed from a
    model: the state after a trade is a hair off the stored `D`, and the
    invariant is sharp enough there that a reconstruction is not to be
    trusted.  Two calls to the chain are.
    """
    step = max(1, int(dx * STEP))
    lo = chain_dy(call, pool, i, j, dx)
    hi = chain_dy(call, pool, i, j, dx + step)
    if lo is None or hi is None or hi <= lo:
        return None
    return ((hi - lo) / 10 ** decimals[j]) / (step / 10 ** decimals[i])


def check(call, pool: str, pair) -> None:
    coins, decimals, balances = shape(call, pool)
    if len(coins) < 2:
        print(f"{pool}  not a pool")
        return
    names = [symbol(call, coin) for coin in coins]
    reading = reading_of(call, pool, decimals, balances)
    pairs = [pair] if pair else [(0, 1), (1, 0)]
    for i, j in pairs:
        head = f"{names[i]}/{names[j]}"
        try:
            fitted = depth.fit(reading, i, j)
        except L.DepthError as exc:
            print(f"{head:22} no curve: {exc}")
            continue
        curve = fitted.curve
        spot_ours = L.spot_price(curve, i, j)
        base = chain_slope(call, pool, decimals, i, j,
                           max(1, balances[i] // 10**7))
        if base is None:
            print(f"{head:22} [{fitted.family}] the chain will not quote")
            continue
        print(f"\n{head:22} [{fitted.family}]   spot: ours {spot_ours:,.8f}"
              f"   chain {base:,.8f}")
        print(f"   {'trade of ' + names[i]:>20}   {'chain p/p0':>12}"
              f"   {'ours p/p0':>12}   {'apart':>8}")
        walked = []
        for share in LADDER:
            dx = int(balances[i] * share)
            if dx < 1:
                continue
            theirs = chain_slope(call, pool, decimals, i, j, dx)
            if theirs is None:
                continue
            moved = curve.xp[i] + dx / 10 ** decimals[i] / curve.scale[i]
            try:
                ours = curve.price_at(i, j, moved)
            except (L.DepthError, ArithmeticError):
                continue
            walked.append((dx / 10 ** decimals[i], theirs, ours))
            print(f"   {dx / 10 ** decimals[i]:>20,.6f}   {theirs / base:>12.6f}"
                  f"   {ours / spot_ours:>12.6f}"
                  f"   {abs(theirs / base / (ours / spot_ours) - 1) * 100:>7.3f}%")
        fee = (reading.get("fee") or 0) / 1e10
        width(curve, i, j, spot_ours, fee, names)
        outputs(call, pool, decimals, curve, reading, i, j, names)


def outputs(call, pool: str, decimals, curve, reading, i: int, j: int,
            names) -> None:
    """Trade to each place and see: what the pool pays, against our curve.

    No differencing anywhere.  Our curve says how much `j` leaves when `dx` of
    `i` arrives -- that is `y_at`, which is the curve itself -- and `get_dy`
    says what the pool actually pays.  The gap between them is the fee, and if
    it lands between `mid_fee` and `out_fee` and grows with the trade, the
    curve is the pool's.
    """
    mid = (reading.get("mid_fee") or 0) / 1e10
    out = (reading.get("out_fee") or 0) / 1e10
    flat = (reading.get("fee") or 0) / 1e10
    print(f"   {'trade of ' + names[i]:>20}   {'chain out':>16}"
          f"   {'ours, gross':>16}   {'implied fee':>11}")
    for share in LADDER:
        dx = int(reading.balances[i] * share)
        if dx < 1:
            continue
        paid = chain_dy(call, pool, i, j, dx)
        if not paid:
            continue
        here = curve.xp[i]
        there = here + dx / 10 ** decimals[i] / curve.scale[i]
        try:
            gross = ((curve.y_at(i, j, here) - curve.y_at(i, j, there))
                     * curve.scale[j])
        except (L.DepthError, ArithmeticError):
            continue
        if gross <= 0:
            continue
        net = paid / 10 ** decimals[j]
        print(f"   {dx / 10 ** decimals[i]:>20,.6f}   {net:>16,.6f}"
              f"   {gross:>16,.6f}   {1 - net / gross:>10.4%}")
    band = f"{mid:.4%}..{out:.4%}" if out else f"{flat:.4%}"
    print(f"   {'':20}   the pool charges between {band}")


def width(curve, i: int, j: int, spot: float, fee: float, names) -> None:
    """Where the depth has fallen to half its peak, against the fee.

    The question the chart raises: a band of liquidity narrower than the fee
    is one no arbitrage can ever reach into.
    """
    try:
        crest = L.peak_price(curve, i, j)
        peak = L.depth_at(curve, i, j, crest)
    except (L.DepthError, ArithmeticError) as exc:
        print(f"   no peak: {exc}")
        return
    floor = L.background(curve, i, j)
    lo, hi = 1e-6, 2.0
    for _ in range(60):
        middle = math.sqrt(lo * hi)
        try:
            here = L.depth_at(curve, i, j, crest * (1 + middle)) - floor
        except (L.DepthError, ArithmeticError):
            hi = middle
            continue
        if here > (peak - floor) * 0.5:
            lo = middle
        else:
            hi = middle
    half = math.sqrt(lo * hi)
    print(f"   peak at {crest / spot - 1:+.4%} from spot, half-width"
          f" {half:.4%}, fee {fee:.4%}"
          f"   -> {'NARROWER than the fee' if half < fee else 'wider than the fee'}")


def main() -> int:
    mainnet, gnosis = endpoints()
    calls = {"ethereum": node(mainnet), "gnosis": node(gnosis) if gnosis else None}
    wanted = DEFAULT
    if len(sys.argv) > 1:
        wanted = []
        for arg in sys.argv[1:]:
            bits = arg.split(":")
            pair = (int(bits[1]), int(bits[2])) if len(bits) > 2 else None
            wanted.append(("ethereum", bits[0], pair))
    for chain, pool, pair in wanted:
        call = calls.get(chain)
        if call is None:
            print(f"\n=== {pool} on {chain}: no endpoint")
            continue
        print(f"\n================ {pool} ({chain})")
        check(call, pool, pair)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
