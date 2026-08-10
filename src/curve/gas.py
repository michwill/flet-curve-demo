"""What a transaction will cost, in the money the user thinks in.

Three numbers multiply into one: how much gas the action burns, what the
chain charges per gas, and what the chain's own coin is worth. Each is
read rather than assumed, and each has a way of being subtly wrong.

**Gas: what it burns, not what is reserved.** A wallet sets a limit above
the estimate so a transaction that costs slightly more than simulated
still lands -- qeth uses `estimate x 1.5`. That headroom is refunded. The
cost to show is the simulation's own figure, and showing the limit would
overstate every fee on the page by half.

**Price: what it will settle at, not the ceiling.** Under EIP-1559 a
wallet names a `maxFeePerGas` it is willing to tolerate and pays
`baseFee + tip`; the difference is refunded. qeth asks for twice the base
fee as headroom, so quoting the ceiling would be a 100% overstatement of
a fee that is, in practice, `baseFee + 5% of baseFee`.

    EIP-1559, baseFee > 0:  baseFee + max(baseFee x 5%, node tip, 1 wei)
    EIP-1559, baseFee == 0: max(gasPrice, node tip, 1 wei)   -- BSC-style
    legacy:                 gasPrice x 1.35

That mirrors `apply_gas_policy` in qeth, which is the wallet the desktop
build talks to. The 1559 arms barely depend on it -- the base fee is the
chain's, and the tip is five per cent of it -- but the legacy arm is
entirely the wallet's markup, and a wallet that marks up differently will
charge differently. It is named here rather than buried for that reason.

**Price of the coin: not every chain runs on ether.** XDAI on Gnosis is a
dollar; POL on Polygon is a fraction of one. A fee quoted in "ETH" on
Gnosis would be wrong by three orders of magnitude, so the native coin is
looked up per chain -- see `NATIVE`.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

#: EIP-7528's stand-in address for a chain's own coin. Curve's price
#: endpoint answers for it on some chains and not others -- see `Native`.
NATIVE_PSEUDO = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

#: The tip, as a fraction of the base fee, held as a ratio rather than
#: `0.05` -- qeth computes `(base * 5) // 100` in integers, and a base fee
#: is large enough that going through a float can land a wei either side
#: of it. The estimate should not disagree with the wallet over rounding.
TIP_NUMERATOR = 5
TIP_DENOMINATOR = 100

#: Below this a tip is refused outright by some chains: Gnosis bounces a
#: transaction whose effective priority fee is under 1 wei, and its base
#: fee is small enough that five per cent of it floors to zero.
MIN_TIP_WEI = 1

#: What a legacy chain's `gasPrice` is multiplied by before it is sent,
#: as qeth's own `(price * 135) // 100`. Unlike the 1559 arms this is
#: entirely wallet policy, and a legacy transaction pays exactly what it
#: names -- there is no refund of the difference -- so it belongs in the
#: estimate.
LEGACY_NUMERATOR = 135
LEGACY_DENOMINATOR = 100


@dataclass(frozen=True, slots=True)
class Native:
    """A chain's own coin, and where its price can be had.

    `wrapped` is the ERC-20 wrapper, used only where the price endpoint
    does not answer for `NATIVE_PSEUDO`. Both are tried in that order and
    the order matters: on Polygon the pseudo-address prices POL at $0.54
    while the wrapped entry reads $0.08, so falling back the other way
    would be sevenfold wrong on a live chain.
    """

    symbol: str
    wrapped: str = ""


#: Keyed by chain id. `symbol` is what the fee is denominated in, which is
#: the whole reason this table exists: three of these are not ether.
NATIVE: dict[int, Native] = {
    1: Native("ETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    10: Native("ETH", "0x4200000000000000000000000000000000000006"),
    56: Native("BNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
    100: Native("XDAI", "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d"),
    137: Native("POL", "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270"),
    252: Native("frxETH", "0xFC00000000000000000000000000000000000006"),
    8453: Native("ETH", "0x4200000000000000000000000000000000000006"),
    42161: Native("ETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
}


def native_for(chain_id: int) -> Native:
    """The chain's coin, or ether where nothing is recorded.

    Ether is the right default rather than a refusal: every chain this app
    reaches that is *not* in the table above is an EVM rollup, and those
    settle in ether almost without exception. A wrong symbol on an unknown
    chain costs a label; refusing to price anything unlisted would cost
    the whole line on chains where it is perfectly correct.
    """
    return NATIVE.get(chain_id) or Native("ETH")


def settlement_price(
    *,
    base_fee: int,
    gas_price: int,
    node_tip: int = 0,
    eip1559: bool = True,
) -> int:
    """Wei per gas the transaction will most likely settle at.

    Not what the wallet asks for -- see the module note. Everything here
    is integer arithmetic on wei: a base fee is not a quantity anyone
    should be dividing in floating point, and the five per cent is a
    floor division exactly as the wallet computes it.
    """
    if not eip1559:
        return gas_price * LEGACY_NUMERATOR // LEGACY_DENOMINATOR
    if base_fee > 0:
        tip = max(
            base_fee * TIP_NUMERATOR // TIP_DENOMINATOR, node_tip, MIN_TIP_WEI
        )
        return base_fee + tip
    # BNB Smart Chain and friends: the base fee is zero and the reported
    # gas price *is* the mandatory tip, so that is the whole cost.
    return max(gas_price, node_tip, MIN_TIP_WEI)


async def read_fees(provider) -> tuple[int, int, int, bool]:
    """`(base fee, gas price, node tip, eip1559)` from the chain itself.

    Three reads, and two of them are allowed to fail. A chain with no
    `baseFeePerGas` in its head block is pre-1559 (or a fork that never
    adopted it) and takes the legacy arm; `eth_maxPriorityFeePerGas` is
    not implemented everywhere and is only ever a floor, so its absence
    costs nothing.
    """
    base = gas_price = tip = 0
    eip1559 = False
    try:
        head = await provider.request("eth_getBlockByNumber", ["latest", False])
        raw = (head or {}).get("baseFeePerGas")
        if raw is not None:
            base = int(raw, 16) if isinstance(raw, str) else int(raw)
            eip1559 = True
    except Exception:
        return 0, 0, 0, False
    with suppress(Exception):
        answer = await provider.request("eth_gasPrice", [])
        gas_price = int(answer, 16) if isinstance(answer, str) else int(answer)
    with suppress(Exception):
        answer = await provider.request("eth_maxPriorityFeePerGas", [])
        tip = int(answer, 16) if isinstance(answer, str) else int(answer)
    return base, gas_price, tip, eip1559


async def native_price(api, chain: str, chain_id: int) -> float:
    """What one unit of the chain's coin is worth, or 0.0.

    Two addresses, in this order and not the other: the price endpoint
    answers for `NATIVE_PSEUDO` on some chains and not others, and where
    it does the answer is the coin itself. The wrapped ERC-20 is the
    fallback for the rest -- Base, Gnosis and Fraxtal return nothing for
    the pseudo-address, and their wrappers are priced correctly.

    Tried the other way round it would be wrong on Polygon, where the
    pseudo-address reads $0.54 for POL and the wrapper reads $0.08.
    """
    price = await api.usd_price(chain, NATIVE_PSEUDO)
    if price > 0:
        return price
    wrapped = native_for(chain_id).wrapped
    return await api.usd_price(chain, wrapped) if wrapped else 0.0


def fee_in_native(gas: int, price_per_gas: int) -> float:
    """The fee as a whole number of the chain's coin."""
    return gas * price_per_gas / 10**18


def format_fee(native: float, symbol: str, usd: float) -> str:
    """`0.00042 ETH  ($0.79)`, or just the coin where no price is known.

    The precision follows the size on *both* halves, because an L2 fee is
    often a thousandth of a cent: rendering those as `$0.00` would make
    the line useless exactly where it is most reassuring, and a fixed six
    decimal places on the coin does the same thing one column earlier --
    0.0000004 ETH is a real fee and reads as `0`.
    """
    if native >= 1:
        places = 4
    elif native >= 0.001:
        places = 6
    elif native >= 10**-6:
        places = 9
    else:
        places = 12
    amount = f"{native:,.{places}f}".rstrip("0").rstrip(".") or "0"
    text = f"{amount} {symbol}"
    if usd <= 0:
        return text
    # Two significant figures, never fewer than two decimal places and
    # never in scientific notation: "$0.79" for a fee worth naming,
    # "$0.00000075" for one that is not, and neither of them "$0.00".
    exponent = Decimal(str(usd)).adjusted()
    return f"{text}  (${usd:,.{max(2, 1 - exponent)}f})"
