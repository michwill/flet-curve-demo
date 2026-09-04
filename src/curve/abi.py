"""Calldata for the Curve contracts this app talks to."""

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
    "decode_uint_array",
    "encode_add_liquidity",
    "encode_allowance",
    "encode_approve",
    "encode_balances_int128",
    "encode_calc_token_amount",
    "encode_calc_withdraw_one_coin",
    "encode_claim_rewards",
    "encode_claim_rewards_for",
    "encode_claimable_reward",
    "encode_claimable_tokens",
    "encode_deposit_and_stake",
    "encode_exchange",
    "encode_gauge_deposit",
    "encode_gauge_withdraw",
    "encode_mint_many",
    "encode_minter_mint",
    "encode_reward_count",
    "encode_reward_tokens",
    "encode_working_balances",
    "encode_get_dy",
    "encode_remove_liquidity",
    "encode_remove_liquidity_one_coin",
    "selector",
]

MAX_UINT256 = (1 << 256) - 1

#: Padding for a fixed-size address array, where a zero slot ends the loop.
#: `curve.stake_zaps` spells the same constant out again rather than
#: importing it from here, because this module is the bottom of the stack.
_ZERO_ADDRESS = "0x" + "0" * 40

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
    """The *tail* of a `uint256[]`: a length word, then the elements."""
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
# `wallet.erc20` covers transfer/balanceOf/decimals/symbol/name.


def encode_approve(spender: str, amount: int) -> str:
    return _call("approve(address,uint256)", _address(spender), _uint(amount))


def encode_allowance(owner: str, spender: str) -> str:
    return _call("allowance(address,address)", _address(owner), _address(spender))


def encode_total_supply() -> str:
    """LP tokens outstanding -- the denominator of a pool share."""
    return _call("totalSupply()")


# -- fees ------------------------------------------------------------------
# Curve states fees as a fraction of 1e10, so 1_500_000 is 0.015%.

#: The denominator Curve's `fee()` is expressed in. 1e10 == 100%.
FEE_DENOMINATOR = 10**10


def encode_fee() -> str:
    """The pool's swap fee. Every pool type has this one."""
    return _call("fee()")


def encode_parameter(name: str) -> str:
    """A no-argument `uint256` getter, by name."""
    return _call(f"{name}()")


def decode_uint_array(data: str) -> list[int]:
    """A `uint256[N]` or a `DynArray[uint256, N]` return value."""
    body = data[2:] if data.startswith("0x") else data
    if not body or len(body) % 64:
        return []
    words = [int(body[i : i + 64], 16) for i in range(0, len(body), 64)]
    if len(words) >= 2 and words[0] == 32 and words[1] == len(words) - 2:
        return words[2:]
    return words


def encode_indexed_parameter(name: str, index: int) -> str:
    """The same getter, on a pool that holds several of them."""
    return _call(f"{name}(uint256)", _uint(index))


def encode_balances_int128(index: int) -> str:
    """`balances(int128)`, the old registry's spelling of one reserve."""
    return _call("balances(int128)", _int(index))


def encode_dynamic_fee(i: int, j: int) -> str:
    """The fee for one particular pair, on the pools that price pairs."""
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
    """Quote a swap between two *underlying* coins of a metapool."""
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
    """Deposit."""
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
    """Estimate LP tokens out. Note the two-argument caveat below."""
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
# A metapool holds two coins: its own, and the base pool's LP token.


def encode_zap_calc_token_amount(
    pool: str | None,
    amounts: list[int],
    *,
    deposit: bool = True,
    dynamic: bool = False,
    stableswap: bool = True,
) -> str:
    """Estimate LP out, in whichever dialect this zap speaks."""
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
# Liquidity gauges take the LP token and mint CRV against it.


def encode_gauge_deposit(amount: int) -> str:
    return _call("deposit(uint256)", _uint(amount))


def encode_gauge_withdraw(amount: int) -> str:
    return _call("withdraw(uint256)", _uint(amount))


# -- claiming --------------------------------------------------------------
# Two halves, because a gauge pays two kinds of reward -- see
# `curve.rewards`.


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


def encode_working_balances(owner: str) -> str:
    """The boosted balance the gauge pays CRV on."""
    return _call("working_balances(address)", _address(owner))


def encode_mint_many(gauges: list[str], slots: int, *, stops_at_zero: bool) -> str:
    """Mint CRV across several gauges in one transaction.

    The array is fixed-width, and **the two implementations do different
    things with the spare slots** -- verified against Curve's own source, not
    inferred from behaviour:

    `Minter.vy`, Ethereum::

        for i in range(8):
            if gauge_addrs[i] == ZERO_ADDRESS:
                break

    `ChildGaugeFactory.vy`, every other chain::

        for i in range(32):
            if _gauges[i] == empty(address):
                pass                       # not `break`
            self._psuedo_mint(_gauges[i], msg.sender)

    So the Minter stops at a zero slot and the child factory walks into
    `_psuedo_mint(0x0)`, where `assert gauge_data != 0  # dev: invalid gauge`
    fails -- a bare assert with the reason in a comment, which is why it
    reaches a wallet as an empty revert.

    `stops_at_zero` says which one this is.  Where it breaks, pad with zeros
    and it does only the work asked of it; where it does not, the spare slots
    have to name a real gauge, and repeating one is the only way to fill them.
    That repeat is *not* free -- every slot does its own
    `user_checkpoint` and `integrate_fraction` on the gauge -- so a repeat is
    a last resort and `encode_minter_mint` is what a single gauge should use.
    Measured on Arbitrum: `mint` 264,620 gas against 2,160,629 for a
    `mint_many` of one gauge repeated 32 times.
    """
    if not gauges:
        raise ValueError("mint_many with no gauges")
    if len(gauges) > slots:
        raise ValueError(f"{len(gauges)} gauges into {slots} slots")
    filler = _ZERO_ADDRESS if stops_at_zero else gauges[-1]
    padded = list(gauges) + [filler] * (slots - len(gauges))
    return _call(
        f"mint_many(address[{slots}])", *(_address(gauge) for gauge in padded)
    )


def encode_claim_rewards_for(owner: str) -> str:
    """Claim a gauge's incentive tokens on behalf of `owner`."""
    return _call("claim_rewards(address)", _address(owner))


def encode_gauge_from_lp_token(lp_token: str) -> str:
    """`get_gauge_from_lp_token(address)` on a child gauge factory.

    How the gauge that is really on this chain is found when the pool list
    names one that is not: the API gives the Ethereum *root* gauge for some
    sidechain pools, and for whole chains gives nothing else.
    """
    return _call("get_gauge_from_lp_token(address)", _address(lp_token))


def encode_gauge_factory() -> str:
    """`factory()` on a child gauge: who is allowed to mint for it.

    Immutable, set when the gauge was deployed.  A chain can have more than
    one child gauge factory, and the gauge's own answer is the only thing
    that says which of them owns it.
    """
    return _call("factory()")


def encode_minter_mint(gauge: str) -> str:
    """Mint the CRV this gauge has recorded for the caller."""
    return _call("mint(address)", _address(gauge))


# -- depositing and staking in one call ------------------------------------
# Curve's deposit-and-stake zap.


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
    """Deposit `amounts` of `coins` and stake the LP that comes back."""
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
    head_words = 7 + len(flags) + 1
    coins_tail = _dyn_address_array(coins)
    return _call(
        signature,
        _address(deposit),
        _address(lp_token),
        _address(gauge),
        _uint(len(coins)),
        _offset(head_words),
        _offset(head_words + 1 + len(coins)),
        _uint(min_mint),
        *flags,
        _address(pool),
        coins_tail,
        _dyn_array(amounts),
    )


# -- veCRV -----------------------------------------------------------------
# The voting escrow and the fee distributor beside it.  `locked` answers two
# words; everything else here is one call and one word.


def encode_locked(owner: str) -> str:
    """`(int128 amount, uint256 end)` -- what this address has locked."""
    return _call("locked(address)", _address(owner))


def decode_locked(data: str) -> tuple[int, int]:
    """That pair, with the amount read as signed: it is an `int128`."""
    words = decode_uint_array(data)
    if len(words) < 2:
        return 0, 0
    # `decode_uint_array` would read the pair as an array header where the
    # amount happens to be 32 and the end 0; a lock of 32 wei that has never
    # been set is not a thing, but be exact rather than nearly.
    amount, end = int(data[2:66], 16), int(data[66:130], 16)
    return (amount - (1 << 256) if amount >> 255 else amount), end


def encode_create_lock(amount: int, unlock_time: int) -> str:
    return _call("create_lock(uint256,uint256)", _uint(amount), _uint(unlock_time))


def encode_increase_amount(amount: int) -> str:
    return _call("increase_amount(uint256)", _uint(amount))


def encode_increase_unlock_time(unlock_time: int) -> str:
    return _call("increase_unlock_time(uint256)", _uint(unlock_time))


def encode_ve_withdraw() -> str:
    """`withdraw()` on the escrow -- takes an expired lock back, all of it."""
    return _call("withdraw()")


def encode_claim(owner: str) -> str:
    """The distributor's `claim`, which is also how the amount is previewed.

    Not a `view`, and answers the amount it would send -- so an `eth_call`
    against it is both the estimate and a dry run of the real thing.
    """
    return _call("claim(address)", _address(owner))


# -- slippage --------------------------------------------------------------


def apply_slippage(amount: int, tolerance_pct: float) -> int:
    """Turn an estimate into the `min_*` floor that goes on-chain."""
    if amount <= 0:
        return 0
    if not 0 <= tolerance_pct < 100:
        raise ValueError(f"slippage must be in [0, 100): {tolerance_pct}")
    scale = 1_000_000
    keep = round((100.0 - tolerance_pct) / 100.0 * scale)
    return amount * keep // scale
