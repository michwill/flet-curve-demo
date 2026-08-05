# Curve API reference

Research notes for building a Curve Finance UI in Python/Flet. Everything here was
verified against the live API on 2026-08-05, not just read from the docs.

## The two APIs

| | Base URL | OpenAPI spec | Covers |
|---|---|---|---|
| **Main API** | `https://api.curve.finance/v1` | [`/v1/openapi.json`](https://api.curve.finance/v1/openapi.json) — 44 endpoints | Pool metadata, TVL, coins, gauges, CRV APY |
| **Prices API** | `https://prices.curve.finance/v1` | [`/feeds-docs/openapi.json`](https://prices.curve.finance/feeds-docs/openapi.json) — 136 endpoints | OHLC, snapshots, volume history, crvUSD, lending, DAO |

Both are fully public — no API key, no auth, no rate-limit headers.

Host notes:
- `api.curve.fi` **301-redirects** to `api.curve.finance`. Use the `.finance` domain.
- On the main API, `/api/…` is a legacy alias for `/v1/…`. Both work and return
  identical payloads. Prefer `/v1/` — it's the one the OpenAPI spec describes.
- The Prices API spec is served from `/feeds-docs/openapi.json`. There is **no**
  spec at `/openapi.json` or `/docs` (both 404).

## ⚠️ Gotcha: User-Agent gating

The API returns **403 Forbidden** to the default `urllib` User-Agent. Measured:

| User-Agent | Status |
|---|---|
| `Python-urllib/3.13` | **403** |
| `python-requests/2.32` | 200 |
| `Python/3.13 aiohttp/3.9` | 200 |
| `flet-curve/0.1` | 200 |

Only the literal `Python-urllib/*` string is blocked, so `requests` / `httpx` /
`aiohttp` work untouched. Set an explicit UA anyway so a stdlib fallback path can
never silently break:

```python
headers = {"User-Agent": "flet-curve/0.1"}
```

## Pool list

`getPools` returns **no volume and no base APY** — only gauge CRV APY. Confirmed
against the full key union of all 382 Ethereum pools. Two calls, joined on
lowercased pool address:

```
GET /v1/getPools/big/{blockchainId}   → pool metadata + TVL
GET /v1/getVolumes/{blockchainId}     → volumeUSD + base APY
```

Join coverage measured at **382/382 = 100%** on Ethereum. Addresses are
checksummed in both responses, but lowercase them before matching anyway.

### Size variants

`getPools` comes in four TVL buckets. Each takes an optional `/{blockchainId}`;
omit it to get every chain in one response.

| Endpoint | Meaning | Ethereum size |
|---|---|---|
| `/v1/getPools/big[/{chain}]` | TVL ≥ $10k | **794 KB** (382 pools) |
| `/v1/getPools/small[/{chain}]` | TVL < $10k | — |
| `/v1/getPools/empty[/{chain}]` | TVL == $0 | — |
| `/v1/getPools/all[/{chain}]` | everything | 4.7 MB |
| `/v1/getPools/{chain}/{registryId}` | one registry | 110 KB (`main`) |

**Use `big`.** It is ~6× smaller than `all` and drops the thousands of dead
zero-TVL pools that would otherwise need client-side filtering.

### Chains and registries

`GET /v1/getPlatforms` returns the chain → registry map plus chain IDs. Build the
chain selector from this rather than hardcoding — coverage changes over time.

21 chains: `ethereum` `polygon` `fantom` `arbitrum` `avalanche` `optimism` `xdai`
`aurora` `harmony` `moonbeam` `kava` `celo` `zkevm` `zksync` `base` `fraxtal`
`bsc` `x-layer` `mantle` `sonic` `hyperliquid`

9 registries: `main` `crypto` `factory` `factory-crypto` `factory-crvusd`
`factory-tricrypto` `factory-twocrypto` `factory-stable-ng` `factory-eywa`

Not every registry exists on every chain — e.g. Ethereum has 8, zksync has 3,
harmony has 2. `factory-eywa` (Fantom only) is currently empty.

Ethereum pool counts by registry: `factory-stable-ng` 1016, `factory-crypto` 401,
`factory-twocrypto` 400, `factory` 381, `factory-tricrypto` 125, `main` 49,
`factory-crvusd` 29, `crypto` 8.

### Pool object fields

From `getPools`:

| Field | Notes |
|---|---|
| `address`, `name`, `symbol` | `symbol` may be empty on some factory pools — fall back to `name` |
| `blockchainId`, `registryId` | Present on `big`/`all`/`small` variants; **absent** on the single-registry endpoint |
| `usdTotal` | Pool TVL in USD. `usdTotalExcludingBasePool` for metapools |
| `coins[]` | `{address, symbol, name, decimals, usdPrice, poolBalance, isBasePoolLpToken}` — enough to render balances with zero RPC calls |
| `gaugeAddress` | `null` when the pool has no gauge |
| `gaugeCrvApy` | `[min, max]` — the 1× → 2.5× veCRV boost range. Often `[0, 0]` |
| `gaugeRewards[]` | Incentive tokens: `{symbol, name, tokenAddress, apy, tokenPrice, decimals}` |
| `poolUrls` | Ready-made deep links: `{swap[], deposit[], withdraw[]}` into the real Curve UI |
| `virtualPrice`, `amplificationCoefficient`, `totalSupply` | Strings holding raw integers — parse carefully |
| `isMetaPool`, `basePoolAddress` | Metapool linkage |
| `assetTypeName` | `usd` / `eth` / `btc` / `crypto` — good for grouping and filters |
| `isBroken` | Filter these out of the UI |
| `implementationAddress`, `zapAddress`, `lpTokenAddress` | |
| `creationTs`, `creationBlockNumber` | |

From `getVolumes` (`data.pools[]`):

`address`, `type`, `volumeUSD`, `latestDailyApyPcent`, `latestWeeklyApyPcent`,
`includedApyPcentFromLsts`, `virtualPrice`. Also `data.totalVolumes` with
chain-wide `totalVolume` / `totalStableVolume` / `totalCryptoVolume`.

Note the naming difference: `getVolumes` returns `…ApyPcent` (already ×100),
while the older `getSubgraphData` returns `latestDailyApy` as a raw fraction.
Prefer `getVolumes` — the spec marks it as the preferred source.

## Charts (Prices API)

The time-series paths are **top-level, not nested under `/pools/`**.
`/v1/pools/{chain}/{addr}/ohlc` returns 404 — that was a wrong guess worth
recording.

```
GET /v1/ohlc/{chain}/{address}
    required: main_token, reference_token, start, end
    optional: agg_number, agg_units
    → {time, open, high, low, close}

GET /v1/snapshots/{chain}/{address}
    required: start, end     optional: unit
    → 27 fields incl. base_daily_apr, base_weekly_apr, virtual_price,
      fee, price_oracle, price_scale, a, gamma, xcp_profit

GET /v1/volume/{chain}/{address}
    required: main_token, reference_token, start, end   optional: interval

GET /v1/usd_price/{chain}/{address}[/history]
```

`main_token` / `reference_token` are the two **coin** addresses to price against
each other, not the pool address. `start` / `end` are Unix seconds.

`/v1/snapshots/…` is the APY-and-TVL-history source for a pool detail view.

## One-shot alternative

`GET /v1/chains/{chain}` on the **Prices** API returns every pool on a chain in a
single call (1298 on Ethereum, 2.4 MB) with `tvl_usd`, `trading_volume_24h`,
`trading_fee_24h`, `trading_fee_24h`, balances and coins — no join required.

Trade-off: it carries **no gauge or CRV APY data**, so it can't drive a rewards
column. Its `page` / `per_page` params are **broken — they return 500 Internal
Server Error**, so it's all-or-nothing.

Chain-wide totals come back under `total`: `total_tvl`, `trading_volume_24h`,
`trading_fee_24h`, `liquidity_volume_24h`, `liquidity_fee_24h`.

## Caching

Responses are Cloudflare-cached with `cache-control: max-age=30, s-maxage=300`.
Polling faster than ~5 min just returns the same cached bytes. Every main-API
response carries `generatedTimeMs`, and payloads are wrapped as:

```json
{ "success": true, "data": { … }, "generatedTimeMs": 123 }
```

The Prices API is **not** wrapped — it returns bare objects, usually with a
`data` array plus `chain` / `count` / `cached_at`.

## Other endpoint groups

Main API:
- `GET /v1/getAllGauges` — every gauge on every chain (2.5 MB), keyed by a
  display name like `"CVX+bveCVX (0x04c9…7512)"`
- `GET /v1/getLendingVaults/all[/{chain}]` — lending vaults.
  Chains: `ethereum` `arbitrum` `optimism` `fraxtal` `sonic`. Registries: `oneway`, `oneway-v2`
- `GET /v1/getHiddenPools` — known-dysfunctional pool IDs by chain, for filtering
- `GET /v1/getTokens/all/{chain}` — all tokens across pools with ≥$10k TVL
- `GET /v1/getPoolList/{chain}` — just addresses
- `GET /v1/getWeeklyFees`, `/v1/getGas`, `/v1/getETHprice`
- crvUSD supply: `/v1/getCrvusdTotalSupply`, `/v1/getScrvusdTotalSupplyNumber`

Prices API groups: `crvusd` (39), `lending` (35), `dao` (25), `refuel` (7),
`chains` (6), `usd_price` (3), `volume` (3), `pools` (3), `liquidity` (3),
`yield_basis` (3), `snapshots` (2), `trades` (2), `ohlc`, `lp_ohlc`, `oracles`, `gas`.

## Deprecated — don't use

The spec explicitly deprecates these in favour of `getPools`:
`getFactoryV2Pools`, `getFactoryCryptoPools`, `getFactoryTVL`,
`getMainRegistryPools`, `getMainRegistryPoolsAndLpTokens`,
`getMainPoolsGaugeRewards`, `getFactoGauges`.

`getFactoryAPYs` and `getMainPoolsAPYs` are documented as returning *inaccurate*
data for chains not indexed by the Prices API or subgraphs. `getFactoryAPYs`
also needs a `/{version}` path segment — without it you get a 301.

## Recommended plan for the Flet UI

1. **Startup** — `GET /v1/getPlatforms` to populate the chain selector.
2. **Pool list** — `GET /v1/getPools/big/{chain}` + `GET /v1/getVolumes/{chain}`,
   joined on lowercased address. Filter out `isBroken`.
3. **Pool detail** — render from the objects already in hand (`coins[]` has
   balances and USD prices), then lazily fetch `/v1/snapshots/…` and `/v1/ohlc/…`
   from the Prices API for charts.
4. Cache responses for ~5 min client-side to match the CDN.

See [`examples/fetch_pools.py`](examples/fetch_pools.py) for a working,
dependency-free implementation of steps 1–2.
