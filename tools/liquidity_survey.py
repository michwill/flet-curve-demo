"""Trace `curve.liquidity` over real pools, and check each against the chain.

The unit tests use synthetic pools, because what wants testing there is the
geometry rather than the arithmetic.  This is the other half: every model is
confirmed against the marginal price the pool's own `get_dy` implies for a
small trade.  That settles which family a pool belongs to without a table of
addresses -- the same check whether the answer is stableswap, an FX swap or a
cryptoswap -- and it is what found the two Gnosis EURe pools and the
YieldBasis BTC pools to be FX swaps while YB/crvUSD is a plain cryptoswap.

A developer tool rather than part of `tools/check.py`: it wants a node.

    .venv/bin/python tools/liquidity_survey.py

`networks.py` from the electric-router checkout is used where it is there,
which is where the endpoints already live; otherwise set `RPC` (and
`RPC_GNOSIS` for the second half).
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from curve import liquidity as L
from erouter.core.keccak import keccak256

#: A trade small enough to read as marginal, as a share of the balance.
PROBE_SHARE = 1_000_000

#: How wide the sparkline is drawn, in characters.
COLUMNS = 46

BLOCKS = "▁▂▃▄▅▆▇█"


def node(url: str):
    """An `eth_call` against one chain, answering `None` where it reverts."""
    def call(to: str, signature: str, *args: int) -> str | None:
        data = ("0x" + keccak256(signature.encode()).hex()[:8]
                + "".join(f"{value:064x}" for value in args))
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }).encode()
        request = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"})
        with urllib.request.urlopen(request, timeout=45) as answer:
            got = json.load(answer)
        return None if "error" in got else got["result"]
    return call


def number(call, to: str, signature: str, *args: int) -> int | None:
    got = call(to, signature, *args)
    return None if got is None else int(got, 16)


def symbol(call, token: str) -> str:
    """A token's symbol, whether it answers with a string or a bytes32."""
    got = call(token, "symbol()")
    if not got:
        return "?"
    body = got[2:]
    try:
        length = int(body[64:128], 16)
        return bytes.fromhex(body[128:128 + length * 2]).decode(errors="replace")
    except (ValueError, IndexError):
        return bytes.fromhex(body[:64]).decode(errors="replace").strip("\x00")


def shape(call, pool: str) -> tuple[list[str], list[int], list[int]]:
    """The coins a pool holds, their decimals and its balances."""
    coins: list[str] = []
    decimals: list[int] = []
    for index in range(8):
        got = call(pool, "coins(uint256)", index)
        if got is None or int(got, 16) == 0:
            break
        token = "0x" + got[-40:]
        coins.append(token)
        decimals.append(number(call, token, "decimals()") or 18)
    balances = [number(call, pool, "balances(uint256)", k)
                for k in range(len(coins))]
    return coins, decimals, [b or 0 for b in balances]


def quoted_price(call, pool: str, i: int, j: int, decimals, balances,
                 crypto: bool) -> float | None:
    """The marginal price the pool itself quotes, with the fee taken back out.

    Both spellings of `get_dy` are tried: which one answers is the same thing
    the router's dialect probe settles, and getting it wrong here would read
    as a model that does not fit.
    """
    dx = max(1, balances[i] // PROBE_SHARE)
    spellings = ["get_dy(uint256,uint256,uint256)", "get_dy(int128,int128,uint256)"]
    if not crypto:
        spellings.reverse()
    for spelling in spellings:
        dy = number(call, pool, spelling, i, j, dx)
        if dy:
            fee = (number(call, pool, "fee()") or 0) / 1e10
            return ((dy / 10 ** decimals[j]) / (dx / 10 ** decimals[i])
                    / (1 - fee))
    return None


def candidates(call, pool: str, decimals, balances):
    """Every model this pool might be, as `(name, crypto, build, seed)`."""
    count = len(balances)
    gamma = number(call, pool, "gamma()")
    if gamma is None:
        amp = number(call, pool, "A()") or 0
        rates = stored_rates(call, pool, count) or [
            10 ** (36 - d) for d in decimals]
        return [("stableswap", False,
                 lambda: L.stableswap_curve(balances, rates, amp * 100, decimals),
                 L.stableswap_seed(amp * 100))]
    invariant = number(call, pool, "D()") or 0
    amp = number(call, pool, "A()") or 0
    precisions = [10 ** (18 - d) for d in decimals]
    seed = L.crypto_seed(gamma, amp, n=count)
    if count == 3:
        scale = [number(call, pool, "price_scale(uint256)", k) or 0
                 for k in range(2)]
        return [
            (f"tricrypto{' legacy' if legacy else ''}", True,
             lambda legacy=legacy: L.tricrypto_curve(
                 balances, precisions, scale, invariant, amp, gamma,
                 legacy=legacy, a_multiplier=100 if legacy else 10_000),
             seed)
            for legacy in (False, True)
        ]
    pegged = number(call, pool, "price_scale()") or 0
    shapes = (("twocrypto", {"stable": False}),
              ("fx swap", {"stable": True}),
              ("twocrypto legacy", {"stable": False, "legacy_pool": True}))
    return [
        (name, True,
         lambda kind=kind: L.twocrypto_curve(
             balances, precisions, pegged, invariant, amp, gamma, **kind),
         seed)
        for name, kind in shapes
    ]


def stored_rates(call, pool: str, count: int) -> list[int] | None:
    """`stored_rates()` where the pool has it, decoded off the dynamic array."""
    got = call(pool, "stored_rates()")
    if not got:
        return None
    body = got[2:]
    try:
        length = int(body[64:128], 16)
        rates = [int(body[128 + 64 * k:192 + 64 * k], 16) for k in range(length)]
    except (ValueError, IndexError):
        return None
    if len(rates) != count or any(rate <= 0 for rate in rates):
        return None
    return rates


def sparkline(found: L.Profile) -> tuple[str, str]:
    """The profile as blocks, and a marker under the column holding spot."""
    peak = found.peak or 1.0
    step = max(1, len(found.samples) // COLUMNS)
    row = "".join(
        BLOCKS[min(len(BLOCKS) - 1,
                   int(found.samples[k].depth / peak * len(BLOCKS)))]
        for k in range(0, len(found.samples), step)
    )
    low = found.samples[0].price
    high = found.samples[-1].price
    at = int(math.log(found.spot / low) / math.log(high / low) * len(row))
    return row, " " * min(max(at, 0), len(row) - 1) + "|"


def survey(url: str, title: str, pools) -> None:
    call = node(url)
    print(f"\n================ {title} ================")
    for pool, (i, j) in pools:
        coins, decimals, balances = shape(call, pool)
        if len(coins) <= max(i, j):
            print(f"{pool}  no such pair")
            continue
        names = [symbol(call, coin) for coin in coins]
        best = None
        for name, crypto, build, seed in candidates(
                call, pool, decimals, balances):
            want = quoted_price(call, pool, i, j, decimals, balances, crypto)
            if want is None or want <= 0:
                continue
            try:
                curve = build()
                got = L.spot_price(curve, i, j)
            except (L.DepthError, ArithmeticError, ValueError):
                continue
            error = abs(got - want) / want
            if best is None or error < best[0]:
                best = (error, name, curve, seed, got, want)
        head = f"{names[i]}/{names[j]}"
        if best is None:
            print(f"{head:34} no model fitted")
            continue
        error, name, curve, seed, got, want = best
        head = f"{head} [{name}]"
        try:
            low, high = L.auto_window(curve, i, j, seed=seed)
            found = L.profile(curve, i, j, low=low, high=high, points=140)
        except L.DepthError as exc:
            print(f"{head:34} spot {got:>16,.8f}  err {error * 100:.3f}%  "
                  f"no profile: {exc}")
            continue
        row, mark = sparkline(found)
        width = high / found.spot - 1
        print(f"{head:34} spot {got:>16,.8f}  chain {want:>16,.8f}  "
              f"err {error * 100:>7.3f}%  window +/-{width:.3e}")
        print(f"{'':34} {row}")
        print(f"{'':34} {mark} spot")


def endpoints() -> tuple[str, str]:
    """Whatever this machine has: the router's `networks.py`, or `$RPC`."""
    for root in (os.environ.get("EROUTER", ""),
                 str(Path.home() / "Projects" / "electric-router")):
        if root and (Path(root) / "networks.py").is_file():
            sys.path.insert(0, root)
            import networks  # type: ignore[import-not-found]
            return networks.NETWORK, getattr(networks, "GNOSIS", "")
    mainnet = os.environ.get("RPC", "")
    if not mainnet:
        raise SystemExit("set RPC=<mainnet endpoint>, or point EROUTER at a "
                         "checkout with networks.py")
    return mainnet, os.environ.get("RPC_GNOSIS", "")


ETHEREUM = [
    ("0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7", (0, 1)),   # 3pool, legacy
    ("0x4dece678ceceb27446b35c672dc7d61f30bad69e", (0, 1)),   # USDC/crvUSD
    ("0x5Ee9606e5611Fd6CE14BD2BC12db70BD53dC9daA", (0, 1)),   # YB/yYB, ng
    ("0xec977F46467a3021785Cff88894886E617abd65b", (0, 1)),   # YB/crvUSD
    ("0x7f86bf177dd4f3494b841a37e810a34dd56c829b", (1, 0)),   # tri WBTC/USDC
    ("0x7f86bf177dd4f3494b841a37e810a34dd56c829b", (2, 1)),   # tri WETH/WBTC
]

#: Curve's stableswap invariant pegged to `price_scale`, at `A_MULTIPLIER`.
YIELD_BASIS = [
    ("0x862CB4E988FB66E72f128d1183829f8c05B6c6A0", (1, 0)),   # cbBTC/crvUSD
    ("0x656341Ef90b622c6634e0573772FfB7f3669b9f3", (1, 0)),   # WETH/crvUSD
    ("0x313698667d7FDD6789a9BC70821309ff891E729A", (1, 0)),   # WBTC/crvUSD
    ("0x4F52C3a81E33521e5a9A47FD9D3BE475D2279c2e", (1, 0)),   # tBTC/crvUSD
]

GNOSIS_POOLS = [
    ("0x056C6C5e684CeC248635eD86033378Cc444459B0", (0, 1)),   # EURe/x3CRV
    ("0x0eCEC6F5276d2Ec6bB864F063D2b76393d6A1A74", (0, 1)),   # USDC.e/EURe
    ("0x9AF34331175e053Bcff330d7Bb7A6ea2bA53e83d", (0, 1)),   # ZCHF/EURe
]


def main() -> int:
    mainnet, gnosis = endpoints()
    survey(mainnet, "ethereum", ETHEREUM)
    survey(mainnet, "yield basis", YIELD_BASIS)
    if gnosis:
        survey(gnosis, "gnosis", GNOSIS_POOLS)
    else:
        print("\nno gnosis endpoint; set RPC_GNOSIS to include those")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
