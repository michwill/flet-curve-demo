"""Calldata for the Curve contracts this app talks to.

Selectors are *computed* here rather than hard-coded, which is the opposite
of what `wallet/erc20.py` does, and the difference is deliberate. ERC-20 has
five fixed signatures. Curve does not: the signature depends on the pool.

  * coin indices are `int128` on StableSwap and `uint256` on CryptoSwap, so
    `exchange` alone has two selectors;
  * `add_liquidity` takes a *fixed-size* array, so a 2-coin pool and a
    3-coin pool are different functions again.

That is dozens of variants, and a wrong one does not fail loudly -- it is
just a call the pool does not implement, which reverts with no message. So
the signature is written out in full at each call site and keccak'd, using
the implementation the wallet package already ships for EIP-55.

ABI note: `uint256[N]` is a *static* type, so it encodes inline as N
consecutive words with no offset/length header. Every payload here is
therefore a plain concatenation of 32-byte words.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # pragma: no cover - direct-script import
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallet.erc20 import decode_uint, keccak256, to_checksum_address  # noqa: E402

__all__ = [
    "selector",
    "encode_approve",
    "encode_allowance",
    "encode_get_dy",
    "encode_exchange",
    "encode_add_liquidity",
    "encode_calc_token_amount",
    "encode_remove_liquidity",
    "encode_remove_liquidity_one_coin",
    "encode_calc_withdraw_one_coin",
    "encode_gauge_deposit",
    "encode_gauge_withdraw",
    "decode_uint",
    "apply_slippage",
    "MAX_UINT256",
]

MAX_UINT256 = (1 << 256) - 1


def selector(signature: str) -> str:
    """First 4 bytes of keccak256 of a canonical signature, as hex."""
    return keccak256(signature.encode()).hex()[:8]


# -- word encoding ---------------------------------------------------------


def _uint(value: int) -> str:
    if value < 0 or value > MAX_UINT256:
        raise ValueError(f"uint256 out of range: {value}")
    return f"{value:064x}"


def _int(value: int) -> str:
    """A signed word. Coin indices are never negative, but be correct anyway."""
    if value < 0:
        return f"{(1 << 256) + value:064x}"
    return _uint(value)


def _address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"not an address: {value!r}")
    return value[2:].lower().rjust(64, "0")


def _bool(value: bool) -> str:
    return _uint(1 if value else 0)


def _array(values: list[int]) -> str:
    """A fixed-size uint256[N]: N words inline, no header."""
    return "".join(_uint(v) for v in values)


def _index(value: int, *, stableswap: bool) -> str:
    """A coin index, in whichever width this pool's ABI declares."""
    return _int(value) if stableswap else _uint(value)


def _call(sig: str, *words: str) -> str:
    return "0x" + selector(sig) + "".join(words)


# -- ERC-20 additions ------------------------------------------------------
#
# `wallet.erc20` covers transfer/balanceOf/decimals/symbol/name. Depositing
# and staking also need the approval pair.


def encode_approve(spender: str, amount: int) -> str:
    return _call("approve(address,uint256)", _address(spender), _uint(amount))


def encode_allowance(owner: str, spender: str) -> str:
    return _call("allowance(address,address)", _address(owner), _address(spender))


def encode_total_supply() -> str:
    """LP tokens outstanding -- the denominator of a pool share.

    Read from the LP token rather than the pool, even though on newer pools
    they are the same contract: `totalSupply()` is ERC-20 and unambiguous,
    while the pool's own `balances` getter is declared `int128` on the old
    registry pools and `uint256` on the new ones.
    """
    return _call("totalSupply()")


# -- fees ------------------------------------------------------------------
#
# Curve states fees as a fraction of 1e10, so 1_500_000 is 0.015%.

#: The denominator Curve's `fee()` is expressed in. 1e10 == 100%.
FEE_DENOMINATOR = 10**10


def encode_fee() -> str:
    """The pool's swap fee. Every pool type has this one."""
    return _call("fee()")


def encode_dynamic_fee(i: int, j: int) -> str:
    """The fee for one particular pair, on the pools that price pairs.

    StableSwap-NG only, and its indices are `int128`, so there is no
    CryptoSwap spelling to dispatch between -- a pool without the method
    answers with a revert (older Vyper) or empty data (newer), and both
    reach the caller as a failed read.

    Verified on mainnet: PayPool (stableswap-ng) answers 1_000_283 where
    its flat `fee()` is 1_000_000; 3pool, stETH-ng and the crypto pools do
    not implement it at all.
    """
    return _call("dynamic_fee(int128,int128)", _int(i), _int(j))


# -- swapping --------------------------------------------------------------


def encode_get_dy(i: int, j: int, dx: int, *, stableswap: bool) -> str:
    """Quote a swap: how much coin `j` comes back for `dx` of coin `i`."""
    sig = (
        "get_dy(int128,int128,uint256)"
        if stableswap
        else "get_dy(uint256,uint256,uint256)"
    )
    return _call(
        sig,
        _index(i, stableswap=stableswap),
        _index(j, stableswap=stableswap),
        _uint(dx),
    )


def encode_exchange(i: int, j: int, dx: int, min_dy: int, *, stableswap: bool) -> str:
    """Swap within the pool. No router, no path -- one pool, two indices."""
    sig = (
        "exchange(int128,int128,uint256,uint256)"
        if stableswap
        else "exchange(uint256,uint256,uint256,uint256)"
    )
    return _call(
        sig,
        _index(i, stableswap=stableswap),
        _index(j, stableswap=stableswap),
        _uint(dx),
        _uint(min_dy),
    )


# -- depositing ------------------------------------------------------------


def encode_add_liquidity(amounts: list[int], min_mint: int) -> str:
    """Deposit. The array's length is part of the signature."""
    n = len(amounts)
    return _call(f"add_liquidity(uint256[{n}],uint256)", _array(amounts), _uint(min_mint))


def encode_calc_token_amount(amounts: list[int], *, deposit: bool = True) -> str:
    """Estimate LP tokens out. Note the two-argument caveat below.

    StableSwap and the NG crypto pools take `(amounts, is_deposit)`. The
    older CryptoSwap pools (`crypto`, `factory-crypto`) declare it with the
    amounts alone. Callers should try this and fall back to
    `encode_calc_token_amount_no_flag` when the call reverts -- the two
    cannot be told apart from API metadata.
    """
    n = len(amounts)
    return _call(
        f"calc_token_amount(uint256[{n}],bool)", _array(amounts), _bool(deposit)
    )


def encode_calc_token_amount_no_flag(amounts: list[int]) -> str:
    """The older CryptoSwap spelling of `calc_token_amount`."""
    n = len(amounts)
    return _call(f"calc_token_amount(uint256[{n}])", _array(amounts))


# -- withdrawing -----------------------------------------------------------


def encode_remove_liquidity(amount: int, min_amounts: list[int]) -> str:
    """Withdraw every coin, in the pool's current proportions."""
    n = len(min_amounts)
    return _call(
        f"remove_liquidity(uint256,uint256[{n}])", _uint(amount), _array(min_amounts)
    )


def encode_remove_liquidity_one_coin(
    amount: int, i: int, min_amount: int, *, stableswap: bool
) -> str:
    """Withdraw the whole position into a single coin."""
    sig = (
        "remove_liquidity_one_coin(uint256,int128,uint256)"
        if stableswap
        else "remove_liquidity_one_coin(uint256,uint256,uint256)"
    )
    return _call(
        sig, _uint(amount), _index(i, stableswap=stableswap), _uint(min_amount)
    )


def encode_calc_withdraw_one_coin(amount: int, i: int, *, stableswap: bool) -> str:
    sig = (
        "calc_withdraw_one_coin(uint256,int128)"
        if stableswap
        else "calc_withdraw_one_coin(uint256,uint256)"
    )
    return _call(sig, _uint(amount), _index(i, stableswap=stableswap))


# -- staking ---------------------------------------------------------------
#
# Liquidity gauges take the LP token and mint CRV against it. `deposit`
# and `withdraw` are the whole staking surface; the balance read is plain
# ERC-20 `balanceOf`, since a gauge is itself a token.


def encode_gauge_deposit(amount: int) -> str:
    return _call("deposit(uint256)", _uint(amount))


def encode_gauge_withdraw(amount: int) -> str:
    return _call("withdraw(uint256)", _uint(amount))


# -- slippage --------------------------------------------------------------


def apply_slippage(amount: int, tolerance_pct: float) -> int:
    """Turn an estimate into the `min_*` floor that goes on-chain.

    Integer math on purpose: this number is a guarantee the contract
    enforces, and float rounding at 1e18 scale is real. Rounds down, so the
    floor is never accidentally set above what the estimate supports.
    """
    if amount <= 0:
        return 0
    if not 0 <= tolerance_pct < 100:
        raise ValueError(f"slippage must be in [0, 100): {tolerance_pct}")
    # basis-point-of-a-basis-point resolution, enough for any UI slider
    scale = 1_000_000
    keep = int(round((100.0 - tolerance_pct) / 100.0 * scale))
    return amount * keep // scale
