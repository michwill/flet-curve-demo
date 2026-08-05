#!/usr/bin/env python3
"""Fetch and rank Curve pools by TVL, joining volume/APY onto pool metadata.

Dependency-free reference implementation of the approach described in
docs/curve-api.md. Run with:  python3 docs/examples/fetch_pools.py [chain]

Note the User-Agent header: the API 403s the default `Python-urllib/*` agent.
"""

import json
import sys
import urllib.request

API = "https://api.curve.finance/v1"
HEADERS = {"User-Agent": "flet-curve/0.1"}


def get(path):
    req = urllib.request.Request(API + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if not payload.get("success"):
        raise RuntimeError(f"API returned success=false for {path}")
    return payload["data"]


def load_pools(chain="ethereum"):
    """Return pools with TVL >= $10k, enriched with 24h volume and base APY."""
    pools = get(f"/getPools/big/{chain}")["poolData"]
    volumes = get(f"/getVolumes/{chain}")["pools"]
    by_address = {v["address"].lower(): v for v in volumes}

    rows = []
    for pool in pools:
        if pool.get("isBroken"):
            continue
        vol = by_address.get(pool["address"].lower(), {})
        crv_apy = pool.get("gaugeCrvApy") or [0, 0]
        rows.append(
            {
                "address": pool["address"],
                "symbol": pool.get("symbol") or pool["name"],
                "registry": pool.get("registryId"),
                "coins": [c["symbol"] for c in pool["coins"]],
                "tvl": pool.get("usdTotal") or 0,
                "volume_24h": vol.get("volumeUSD") or 0,
                "base_apy": vol.get("latestWeeklyApyPcent") or 0,
                "crv_apy": (crv_apy[0] or 0, crv_apy[1] or 0),
                "rewards": [r.get("symbol") for r in pool.get("gaugeRewards") or []],
            }
        )
    rows.sort(key=lambda r: -r["tvl"])
    return rows


def main():
    chain = sys.argv[1] if len(sys.argv) > 1 else "ethereum"
    rows = load_pools(chain)
    total = sum(r["tvl"] for r in rows)
    print(f"{chain}: {len(rows)} pools >= $10k TVL, ${total:,.0f} total\n")

    header = (
        f'{"SYMBOL":<16}{"REGISTRY":<20}{"COINS":<26}'
        f'{"TVL":>14}{"VOL 24H":>13}{"BASE%":>7}{"CRV% min-max":>16}  REWARDS'
    )
    print(header)
    for r in rows[:20]:
        lo, hi = r["crv_apy"]
        print(
            f'{r["symbol"][:15]:<16}{(r["registry"] or "")[:19]:<20}'
            f'{"/".join(r["coins"])[:25]:<26}'
            f'{r["tvl"]:>14,.0f}{r["volume_24h"]:>13,.0f}{r["base_apy"]:>7.2f}'
            f'{lo:>8.2f}-{hi:<7.2f}  {",".join(r["rewards"])}'
        )


if __name__ == "__main__":
    main()
