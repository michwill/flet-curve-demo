"""The numbers that define a pool's curve, and how to read each one.

Curve's pools are one equation with several parameters, and which
parameters exist depends on the family: a StableSwap pool has `A` and a
flat fee, a CryptoSwap pool has `A`, `gamma`, a fee that moves between
`mid_fee` and `out_fee` as the pool goes off balance, and an internal
price it repegs around. None of it is derivable from the registry name --
a factory can deploy a pool of either shape -- so this app asks the
contract and shows what answers, the same way it settles every other
dialect question here.

**Every value is scaled**, and not all by the same thing, which is the
part worth writing down. Measured on mainnet rather than taken from the
docs:

    pool                     A        gamma          fee        other
    3pool (main)             4000     --             1500000    admin_fee 1e10
    stETH-ng (factory)       1500     --             800000
    PayPool (stableswapng)   5000     --             1000000    offpeg 1e11
    tricryptoUSDC            1707629  11809167828997 3825607    mid 3000000
                                                                out 30000000
                                                                fee_gamma 5e14
    YB cbBTC (twocryptong)   50000    11111111111    146000000

So: fees are tenth-of-a-basis-point integers (`1e10` == 100%, so 1500000
is 0.015%); `gamma`, `fee_gamma` and the prices are 1e18 fixed point; the
off-peg fee multiplier is 1e10 fixed point (1e11 is a 10x multiplier);
and `A` is a plain integer that already includes whatever multiplier its
family uses -- 4000 for a stable pool, 1707629 for a tricrypto one, and
both are what Curve's own UI shows.

`price_oracle` and `price_scale` are spelled two ways. Tricrypto pools
hold several prices and take an index, `price_oracle(uint256)`; twocrypto
and the stable factories hold one and take none. Neither is inferable
from the pool type as the API reports it, so both are tried -- the same
"ask, do not guess" the ABI dialects get.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .format import percent

#: `fee()` and friends are fractions of this. 1e10 == 100%.
FEE_DENOMINATOR = 10**10

#: Vyper's fixed-point one, for `gamma`, `fee_gamma` and the prices.
PRECISION = 10**18


class Kind(Enum):
    """How a raw integer becomes something worth reading."""

    #: A plain count. `A` is this: it carries its family's multiplier and
    #: is shown the way Curve shows it.
    INTEGER = "integer"
    #: A fraction of `FEE_DENOMINATOR`, shown as a percentage.
    PERCENT = "percent"
    #: 1e18 fixed point, shown as a number.
    RATIO = "ratio"
    #: 1e10 fixed point, shown as "10x".
    MULTIPLIER = "multiplier"


@dataclass(frozen=True)
class Parameter:
    """One number, and what to call it."""

    key: str
    label: str
    kind: Kind
    note: str = ""


#: In the order they are worth reading: the shape of the curve first, then
#: what it charges, then where it thinks the price is.
PARAMETERS: tuple[Parameter, ...] = (
    Parameter("A", "A", Kind.INTEGER, "Amplification: how flat the curve is near balance"),
    Parameter("gamma", "gamma", Kind.RATIO, "How quickly the curve leaves the peg"),
    Parameter("fee", "Fee", Kind.PERCENT, "What a swap pays right now"),
    Parameter("mid_fee", "Mid fee", Kind.PERCENT, "The fee when the pool is balanced"),
    Parameter("out_fee", "Out fee", Kind.PERCENT, "The fee when it is far from balance"),
    Parameter("fee_gamma", "Fee gamma", Kind.RATIO, "How fast the fee moves between the two"),
    Parameter(
        "offpeg_fee_multiplier",
        "Fee multiplier",
        Kind.MULTIPLIER,
        "How much the fee rises off peg",
    ),
    Parameter("price_oracle", "Price oracle", Kind.RATIO, "The pool's own moving-average price"),
    Parameter("price_scale", "Price scale", Kind.RATIO, "The price the curve is currently pegged to"),
)

#: Keyed, for the reader.
BY_KEY = {parameter.key: parameter for parameter in PARAMETERS}


def format_value(kind: Kind, raw: int) -> str:
    """A raw contract integer as something to put in a row."""
    if kind is Kind.INTEGER:
        return f"{raw:,}"
    if kind is Kind.PERCENT:
        return percent(raw / FEE_DENOMINATOR * 100, places=4)
    if kind is Kind.MULTIPLIER:
        # "10x", not "x10" with a multiplication sign: the web build's
        # font has no glyph for U+00D7 and would draw a tofu box.
        return f"{raw / FEE_DENOMINATOR:,.4g}x"
    return f"{raw / PRECISION:,.6g}"


def rows(values: dict[str, int]) -> list[tuple[Parameter, str]]:
    """The parameters this pool answered, formatted, in table order.

    Whatever did not answer is simply absent: a pool with no `gamma` is a
    StableSwap pool, not a broken read, and a row saying "gamma: —" would
    invite the question of what it would have been.
    """
    return [
        (parameter, format_value(parameter.kind, values[parameter.key]))
        for parameter in PARAMETERS
        if parameter.key in values
    ]
