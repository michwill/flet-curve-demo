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

The addresses, because "PayPool" is not enough to re-read a row by and
guessing at one wastes an afternoon proving the table wrong when what was
actually wrong was the address:

    3pool          0xbEbc44782C7dB0a1A60Cb6fe97d0b483032FF1C7
    stETH-ng       0x21E27a5E5513D6e65C4f830167390997aA84843a
    PayPool        0x383E6b4437b59fff47B619CBA855CA29342A8559
    tricryptoUSDC  0x7F86Bf177Dd4F3494b841a37e810A34dD56c829B

`get_virtual_price` is the odd one out and is here anyway. It is not a
parameter of the curve, it is what the curve has *done*: the value of one
LP token, 1e18 fixed point, starting at exactly one when the pool is
deployed. Every family implements it, unlike everything above -- read on
mainnet at block 25766403:

    3pool         1039823717356571085    tricryptoUSDC 1035070122939955419
    stETH-ng      1078054572865277880    tricrypto2    1071540298809357145
    PayPool       1044342134767685613    sUSD (old)    1148714008780365037

-- which is why it gets a scale of its own. `Kind.RATIO`'s six significant
digits render all of those as `1.03982`, `1.07805` and so on, and, worse,
render three *different* readings of the same pool identically: the whole
reason to look at this number is the digits six places further out. See
`PRECISE_PLACES`.

Implemented everywhere is not the same as answered everywhere: it is
`D * PRECISION / totalSupply`, so a pool nobody has deposited into
reverts on the division. DOLA/FRAXPYUSD at
0x007ECFD6342C0Cc12A2f0928eDbeE8bFAF675185 does exactly that. It comes
back as absence, like any other unanswered read, which is the right
answer -- an empty pool has no LP token to value.

`price_oracle` and `price_scale` are spelled two ways. Tricrypto pools
hold several prices and take an index, `price_oracle(uint256)`; twocrypto
and the stable factories hold one and take none. Neither is inferable
from the pool type as the API reports it, so both are tried -- the same
"ask, do not guess" the ABI dialects get.

`stored_rates` is spelled two ways as well, and worse: it is the only
read here that answers an *array*, and the array comes back in either of
the two encodings a Vyper `uint256[N]` and a `DynArray[uint256, N]`
produce. stETH-ng answers the first, a stableswap-ng pool the second, and
`abi.decode_uint_array` sniffs which. It also lives outside `PARAMETERS`
for that reason -- see `rate_rows`, and `pool.ARRAY_PARAMETERS` for how it
still rides in the same batch.

Surveyed across all 2,009 pools the API lists for mainnet, 1,011 answer
it -- and 541 of those price every coin identically, which is a pool with
no rate oracle between its coins and shows no rows at all. So the row is
a minority one twice over.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .format import percent

#: `fee()` and friends are fractions of this. 1e10 == 100%.
FEE_DENOMINATOR = 10**10

#: Vyper's fixed-point one, for `gamma`, `fee_gamma` and the prices.
PRECISION = 10**18

#: What `stored_rates()` scales every coin to, whatever its own decimals.
#: So the divisor is `10 ** (36 - decimals)` and differs per coin -- see
#: `rate_rows`, which is the only thing that gets this right or wrong.
RATE_DECIMALS = 36

#: What a rate row is called, before the pair. It names where the number
#: came from, which is the one thing the pair alone does not say -- and it
#: separates these rows from `price_oracle` directly above them, which is
#: the pool's *own* moving average of its *own* trades. Two rows a line
#: apart, both called some kind of oracle, measuring different things from
#: different places.
RATE_LABEL = "External oracle"

#: Decimal places for `Kind.PRECISE`, which in practice means the virtual
#: price. Twelve, because that is where the movement is: a pool earning 5%
#: a year gains about 1.9e-8 of virtual price per twelve-second block --
#: the eighth place -- and a quiet pool far less than that. Not the full
#: eighteen, which is exact but is twenty characters in a row that has a
#: label beside it, and the last six of them have never yet been the
#: reason anybody opened this fold.
PRECISE_PLACES = 12


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
    #: 1e18 fixed point again, but shown to `PRECISE_PLACES` rather than to
    #: six significant digits: a number that sits near 1.0 and whose whole
    #: interest is how far it has crept away from it.
    PRECISE = "precise"


@dataclass(frozen=True)
class Parameter:
    """One number, and what to call it."""

    key: str
    label: str
    kind: Kind
    note: str = ""


#: In the order they are worth reading: the shape of the curve first, then
#: what it charges, then where it thinks the price is, and last what all
#: of that has added up to.
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
    Parameter(
        "get_virtual_price",
        "Virtual price",
        Kind.PRECISE,
        "What one LP token is worth, from 1.0 at launch",
    ),
)

#: Keyed, for the reader.
BY_KEY = {parameter.key: parameter for parameter in PARAMETERS}


@dataclass(frozen=True)
class Readings:
    """One batch of reads, in the two shapes that come back.

    `values` is the single words -- everything in `PARAMETERS`. `rates` is
    `stored_rates()`, which is an array and so cannot share the dict
    without lying about its type. Both are absent-if-unanswered: a pool
    with no rate oracle machinery leaves `rates` empty, and that is a fact
    about the pool rather than a failed read.

    Falsy when nothing at all answered, so the caller can say so.
    """

    values: dict[str, int] = field(default_factory=dict)
    rates: tuple[int, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.values or self.rates)


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
    if kind is Kind.PRECISE:
        # `Decimal`, not float division. Twelve places of a value near one
        # is thirteen significant digits and a float carries about sixteen,
        # so this is a narrow point: measured over 200,000 values, `raw /
        # PRECISION` lands on the wrong side of the twelfth place about one
        # time in 25,000 -- when the exact value sits within an ulp of a
        # rounding boundary. Rare, wrong in exactly the digit this kind
        # exists to show, and free to avoid on a number read once a panel.
        return f"{Decimal(raw) / PRECISION:,.{PRECISE_PLACES}f}"
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


def rate_rows(
    rates: Sequence[int], coins: Sequence[tuple[str, int]]
) -> list[tuple[Parameter, str]]:
    """`stored_rates()` as a price per coin, against the first one.

    The rate a pool prices each of its coins at, from whatever oracle sits
    behind it -- an LST's exchange rate, a yield-bearing stablecoin's share
    price. Shown as a **ratio to the first coin's rate**, and labelled that
    way (`External oracle sUSDe/DOLA`), which is what makes the number a
    price you can read rather than a multiplier you have to divide
    yourself. On the prefix, see `RATE_LABEL`.

    The ratio matters, and this is the part that is easy to get wrong.
    `stored_rates` is denominated in the pool's own accounting unit, not in
    coin 0, and those coincide only when coin 0 has no oracle of its own.
    Surveyed across all 2,009 mainnet pools, 1,011 answer this method and
    **298 of them -- 29% -- have a first rate that is not 1.0**:

        osETH-rETH   osETH  1.077151698614    rETH   1.169697850261
        ETHxwstETH   wstETH 1.241737624641    ETHx   1.095114323123
        wbIB01-$lp   wbIB01 121.520000000000  FRAX   1.000000000000

    So printing the raw rate under a `rETH/osETH` label would claim rETH is
    worth 1.1697 osETH when the pool prices it at 1.0859. Dividing makes the
    label true, and where coin 0 *is* the numeraire -- the other 71% -- it
    changes nothing, because dividing by one is free.

    Coin 0's own row is dropped: against itself it is 1.0 by construction,
    and a row that can only ever say one thing says nothing.

    Everything at exactly 1.0 returns nothing at all. That is the 541 pools
    of the 1,011 whose coins have no rate oracle between them, where the
    rows would be a column of `1.000000000000` restating that this pool is
    the ordinary kind.

    **Each coin has its own denominator.** The contract scales every coin
    to 36 decimals, so the raw word is `10 ** (36 - decimals) * rate`, and
    USDC's flat 1.0 arrives as 1e30 where DOLA's arrives as 1e18. One
    shared 1e18 would print USDC's rate as a trillion.

    Which is why a length mismatch returns nothing at all. `coins` must be
    the *contract's* coins -- `Pool.pool_coins`, two on a metapool, not the
    four `coins` decomposes it into -- and if it is not, the zip would pair
    each rate with the wrong denominator and produce a number that is
    wrong by orders of magnitude while looking entirely plausible.
    """
    if not rates or len(rates) != len(coins) or not rates[0]:
        return []
    base, (base_symbol, base_decimals) = rates[0], coins[0]
    shown: list[tuple[Parameter, str]] = []
    for index in range(1, len(rates)):
        symbol, decimals = coins[index]
        # `rate[i] / 10**(36-di)` over `rate[0] / 10**(36-d0)`, kept in
        # integers and scaled to 1e18 for `Kind.PRECISE`. The truncation is
        # at 1e-18, six places past anything shown.
        scaled = (
            PRECISION * rates[index] * 10 ** (RATE_DECIMALS - base_decimals)
        ) // (base * 10 ** (RATE_DECIMALS - decimals))
        shown.append(
            (
                Parameter(
                    f"stored_rates[{index}]",
                    f"{RATE_LABEL} {symbol}/{base_symbol}",
                    Kind.PRECISE,
                    "stored_rates(): the outside price this pool has cached",
                ),
                format_value(Kind.PRECISE, scaled),
            )
        )
    flat = format_value(Kind.PRECISE, PRECISION)
    return [] if all(value == flat for _parameter, value in shown) else shown
