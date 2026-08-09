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

from wallet.erc20 import decode_uint, keccak256

__all__ = [
    "MAX_UINT256",
    "apply_slippage",
    "decode_uint",
    "encode_add_liquidity",
    "encode_allowance",
    "encode_approve",
    "encode_calc_token_amount",
    "encode_calc_withdraw_one_coin",
    "encode_claim_rewards",
    "encode_claimable_reward",
    "encode_claimable_tokens",
    "encode_deposit_and_stake",
    "encode_exchange",
    "encode_gauge_deposit",
    "encode_gauge_withdraw",
    "encode_minter_mint",
    "encode_reward_count",
    "encode_reward_tokens",
    "encode_get_dy",
    "encode_remove_liquidity",
    "encode_remove_liquidity_one_coin",
    "selector",
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


def _dyn_array(values: list[int]) -> str:
    """The *tail* of a `uint256[]`: a length word, then the elements.

    Dynamic arguments are split in two. The head, in argument order, holds
    a byte offset from the start of the argument block; the tail holds the
    data. So a caller has to place the offset itself -- which is why this
    returns only the tail and every dynamic encoder below spells out its
    own head.
    """
    return _uint(len(values)) + _array(values)


def _offset(words: int) -> str:
    """A head word pointing at the tail, given the head's length in words."""
    return _uint(words * 32)


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


def encode_parameter(name: str) -> str:
    """A no-argument `uint256` getter, by name.

    All the pool parameters are shaped alike -- `A()`, `gamma()`,
    `mid_fee()`, `offpeg_fee_multiplier()` -- so they need one encoder
    rather than nine. Which of them a given pool implements is a question
    for the pool; see `curve.parameters`.
    """
    return _call(f"{name}()")


def encode_indexed_parameter(name: str, index: int) -> str:
    """The same getter, on a pool that holds several of them.

    Tricrypto pools take an index for `price_oracle` and `price_scale`;
    twocrypto and the stable factories take none. There is no way to tell
    from the registry, so callers try both.
    """
    return _call(f"{name}(uint256)", _uint(index))


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


def encode_get_dy_underlying(i: int, j: int, dx: int, *, stableswap: bool) -> str:
    """Quote a swap between two *underlying* coins of a metapool.

    A metapool holds its own coin and the base pool's LP token, but it can
    swap anything in the base pool for its own coin in one call: it does
    the base-pool leg itself. No zap, no approval to anything but the pool
    -- which is why this route exists on every chain, including the ones
    with no zap deployed at all.

    Indices are into the *underlying* list -- the metapool's coin, then the
    base pool's -- rather than the two the contract holds.
    """
    return _call(
        "get_dy_underlying(int128,int128,uint256)"
        if stableswap
        else "get_dy_underlying(uint256,uint256,uint256)",
        _index(i, stableswap=stableswap),
        _index(j, stableswap=stableswap),
        _uint(dx),
    )


def encode_exchange_underlying(
    i: int, j: int, dx: int, min_dy: int, *, stableswap: bool
) -> str:
    """The swap `get_dy_underlying` quotes."""
    return _call(
        "exchange_underlying(int128,int128,uint256,uint256)"
        if stableswap
        else "exchange_underlying(uint256,uint256,uint256,uint256)",
        _index(i, stableswap=stableswap),
        _index(j, stableswap=stableswap),
        _uint(dx),
        _uint(min_dy),
    )


# -- depositing ------------------------------------------------------------


def encode_add_liquidity(
    amounts: list[int], min_mint: int, *, dynamic: bool = False
) -> str:
    """Deposit.

    Two spellings, and they are different functions rather than different
    encodings of one: on the classic pools the array is `uint256[N]`, so
    its length is part of the signature; on StableSwap-NG it is a
    `DynArray`, so the signature is length-agnostic and the calldata
    carries an offset and a length. Sending one to a pool that speaks the
    other reverts.
    """
    if dynamic:
        return _call(
            "add_liquidity(uint256[],uint256)",
            _offset(2),
            _uint(min_mint),
            _dyn_array(amounts),
        )
    n = len(amounts)
    return _call(f"add_liquidity(uint256[{n}],uint256)", _array(amounts), _uint(min_mint))


def encode_calc_token_amount(
    amounts: list[int], *, deposit: bool = True, dynamic: bool = False
) -> str:
    """Estimate LP tokens out. Note the two-argument caveat below.

    StableSwap and the NG crypto pools take `(amounts, is_deposit)`. The
    older CryptoSwap pools (`crypto`, `factory-crypto`) declare it with the
    amounts alone. Callers should try this and fall back to
    `encode_calc_token_amount_no_flag` when the call reverts -- the two
    cannot be told apart from API metadata.
    """
    if dynamic:
        return _call(
            "calc_token_amount(uint256[],bool)",
            _offset(2),
            _bool(deposit),
            _dyn_array(amounts),
        )
    n = len(amounts)
    return _call(
        f"calc_token_amount(uint256[{n}],bool)", _array(amounts), _bool(deposit)
    )


def encode_calc_token_amount_no_flag(amounts: list[int]) -> str:
    """The older CryptoSwap spelling of `calc_token_amount`."""
    n = len(amounts)
    return _call(f"calc_token_amount(uint256[{n}])", _array(amounts))


# -- metapools, through a zap ----------------------------------------------
#
# A metapool holds two coins: its own, and the base pool's LP token. So
# depositing USDC into a USD1/crv2pool metapool means minting crv2pool
# first, which is two transactions and a token nobody wanted to hold.
#
# The zap does both in one call. It takes the *underlying* list -- the
# metapool's own coin followed by the base pool's coins -- and every
# function carries the pool address as its first argument, because one zap
# serves every metapool from its factory.
#
# The `uint256[]`/`uint256[N]` split is the same one the pools have, and
# for the same reason: the StableSwap-NG zap was written against NG pools.
# Both dialects are verified against deployed Ethereum zaps --
# 0xE07a16358aA878CBDa2D49A88E5106871E0db307 (dynamic) and
# 0xA79828DF1850E8a3A3064576f380D90aECDD3359 (fixed, 3pool metapools) --
# by running a deposit and a withdrawal through each on a mainnet fork.
#
# Only the arrays differ. `calc_withdraw_one_coin` and
# `remove_liquidity_one_coin` are identical in both, `int128` included:
# every metapool with a zap is a StableSwap pool.


def encode_zap_calc_token_amount(
    pool: str | None,
    amounts: list[int],
    *,
    deposit: bool = True,
    dynamic: bool = False,
    stableswap: bool = True,
) -> str:
    """Estimate LP out, in whichever dialect this zap speaks.

    Three axes, all of them observed on deployed zaps:

      * `dynamic` -- StableSwap-NG's `uint256[]` against everyone else's
        `uint256[N]`;
      * `pool` -- a factory zap serves every metapool on its base pool and
        so takes the pool address; a per-pool zap is deployed for one pool
        and takes none. `None` means the latter;
      * `stableswap` -- the stable zaps take the `is_deposit` flag that
        the pools take, and the crypto ones do not have it at all.
    """
    if dynamic:
        return _call(
            "calc_token_amount(address,uint256[],bool)",
            _address(pool or ""),
            _offset(3),
            _bool(deposit),
            _dyn_array(amounts),
        )
    n = len(amounts)
    flag = [_bool(deposit)] if stableswap else []
    if pool is None:
        return _call(
            f"calc_token_amount(uint256[{n}]{',bool' if stableswap else ''})",
            _array(amounts),
            *flag,
        )
    return _call(
        f"calc_token_amount(address,uint256[{n}]{',bool' if stableswap else ''})",
        _address(pool),
        _array(amounts),
        *flag,
    )


def encode_zap_add_liquidity(
    pool: str | None, amounts: list[int], min_mint: int, *, dynamic: bool = False
) -> str:
    """Deposit through a zap. Same shape in every dialect but the array."""
    if dynamic:
        return _call(
            "add_liquidity(address,uint256[],uint256)",
            _address(pool or ""),
            _offset(3),
            _uint(min_mint),
            _dyn_array(amounts),
        )
    n = len(amounts)
    if pool is None:
        return _call(
            f"add_liquidity(uint256[{n}],uint256)", _array(amounts), _uint(min_mint)
        )
    return _call(
        f"add_liquidity(address,uint256[{n}],uint256)",
        _address(pool),
        _array(amounts),
        _uint(min_mint),
    )


def encode_zap_remove_liquidity(
    pool: str | None, amount: int, min_amounts: list[int], *, dynamic: bool = False
) -> str:
    if dynamic:
        return _call(
            "remove_liquidity(address,uint256,uint256[])",
            _address(pool or ""),
            _uint(amount),
            _offset(3),
            _dyn_array(min_amounts),
        )
    n = len(min_amounts)
    if pool is None:
        return _call(
            f"remove_liquidity(uint256,uint256[{n}])", _uint(amount), _array(min_amounts)
        )
    return _call(
        f"remove_liquidity(address,uint256,uint256[{n}])",
        _address(pool),
        _uint(amount),
        _array(min_amounts),
    )


def encode_zap_calc_withdraw_one_coin(
    pool: str | None, amount: int, i: int, *, stableswap: bool = True
) -> str:
    """The coin index takes the same two widths the pools' own do."""
    kind = "int128" if stableswap else "uint256"
    index = _index(i, stableswap=stableswap)
    if pool is None:
        return _call(
            f"calc_withdraw_one_coin(uint256,{kind})", _uint(amount), index
        )
    return _call(
        f"calc_withdraw_one_coin(address,uint256,{kind})",
        _address(pool),
        _uint(amount),
        index,
    )


def encode_zap_remove_liquidity_one_coin(
    pool: str | None, amount: int, i: int, min_out: int, *, stableswap: bool = True
) -> str:
    kind = "int128" if stableswap else "uint256"
    index = _index(i, stableswap=stableswap)
    if pool is None:
        return _call(
            f"remove_liquidity_one_coin(uint256,{kind},uint256)",
            _uint(amount),
            index,
            _uint(min_out),
        )
    return _call(
        f"remove_liquidity_one_coin(address,uint256,{kind},uint256)",
        _address(pool),
        _uint(amount),
        index,
        _uint(min_out),
    )


# -- withdrawing -----------------------------------------------------------


def encode_remove_liquidity(
    amount: int, min_amounts: list[int], *, dynamic: bool = False
) -> str:
    """Withdraw every coin, in the pool's current proportions."""
    if dynamic:
        return _call(
            "remove_liquidity(uint256,uint256[])",
            _uint(amount),
            _offset(2),
            _dyn_array(min_amounts),
        )
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


# -- claiming --------------------------------------------------------------
#
# Two halves, because a gauge pays two kinds of reward -- see
# `curve.rewards`. The one thing to know here is that
# `claimable_tokens(address)` is `nonpayable` in the ABI while
# `claimable_reward(address,address)` is `view`: the CRV figure
# checkpoints before it answers, so a typed client tries to *send* it.
# Both are read with `eth_call` here, which is what makes the difference
# invisible to the caller -- the checkpoint happens in a simulated state
# and is discarded, and the return value is the number to display.


def encode_claimable_tokens(owner: str) -> str:
    """CRV owed. `nonpayable` in the ABI, an `eth_call` in practice."""
    return _call("claimable_tokens(address)", _address(owner))


def encode_claimable_reward(owner: str, token: str) -> str:
    """One incentive token's outstanding amount. A genuine view."""
    return _call("claimable_reward(address,address)", _address(owner), _address(token))


def encode_reward_count() -> str:
    return _call("reward_count()")


def encode_reward_tokens(index: int) -> str:
    return _call("reward_tokens(uint256)", _uint(index))


def encode_claim_rewards() -> str:
    """Every incentive token at once. CRV is not among them."""
    return _call("claim_rewards()")


def encode_minter_mint(gauge: str) -> str:
    """Mint the CRV this gauge has recorded for the caller.

    Sent to the Minter on Ethereum and to the child gauge factory
    elsewhere; the signature is the same on both.
    """
    return _call("mint(address)", _address(gauge))


# -- depositing and staking in one call ------------------------------------
#
# Curve's deposit-and-stake zap. Two dynamic arrays in one argument list,
# which is the only place in this file where two heads point at two tails,
# so the offsets are worked out rather than written down: the second tail
# begins after the first one's length word and its elements.
#
# The two spellings are `curve.stake_zaps`' business, not this file's --
# see that module for which chains take which, and why guessing is a
# selector for a function that does not exist.


def _dyn_address_array(values: list[str]) -> str:
    """The tail of an `address[]`: a length word, then one word each."""
    return _uint(len(values)) + "".join(_address(v) for v in values)


def encode_deposit_and_stake(
    deposit: str,
    lp_token: str,
    gauge: str,
    coins: list[str],
    amounts: list[int],
    min_mint: int,
    *,
    use_dynarray: bool,
    pool: str,
    use_underlying: bool | None = None,
) -> str:
    """Deposit `amounts` of `coins` and stake the LP that comes back.

    `deposit` is where the coins go -- the pool itself, or its deposit zap
    on the underlying route -- and `pool` is the metapool a factory zap has
    to be told about, or the zero address when there is none. They are two
    arguments because they are two different things: the zap is what gets
    called, the pool is what it is called *for*.

    `use_underlying` present means the ten-argument spelling; None means
    the nine-argument one. Not a default, because there is no safe default:
    sending the wrong arity is calldata for a function the contract does
    not have.
    """
    if len(coins) != len(amounts):
        raise ValueError(f"{len(coins)} coins against {len(amounts)} amounts")
    flags = (
        [_bool(use_underlying), _bool(use_dynarray)]
        if use_underlying is not None
        else [_bool(use_dynarray)]
    )
    signature = (
        "deposit_and_stake(address,address,address,uint256,address[],uint256[],"
        + ("uint256,bool,bool,address)" if use_underlying is not None else "uint256,bool,address)")
    )
    # Head: three addresses, n_coins, two offsets, min_mint, the flags, pool.
    head_words = 7 + len(flags) + 1
    coins_tail = _dyn_address_array(coins)
    return _call(
        signature,
        _address(deposit),
        _address(lp_token),
        _address(gauge),
        _uint(len(coins)),
        _offset(head_words),
        # Past the coins tail: its length word plus one word per address.
        _offset(head_words + 1 + len(coins)),
        _uint(min_mint),
        *flags,
        _address(pool),
        coins_tail,
        _dyn_array(amounts),
    )


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
    keep = round((100.0 - tolerance_pct) / 100.0 * scale)
    return amount * keep // scale
