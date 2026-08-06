# Curve API reference

Research notes for building a Curve Finance UI in Python/Flet. Everything here
was verified against the live API, not just read from the docs.

**This app uses Prices API v2 for pool data, v1 for charts, and the Lite API
for the chains v2 does not cover.** The v1 main API (`api.curve.finance`) is
documented at the end for reference; it is used only for the metapool zap
addresses, which nothing else publishes.

## The four surfaces

| | Base URL | Spec | Used for |
|---|---|---|---|
| **Prices v2** | `https://prices.curve.finance/v2` | [`/v2/docs/openapi.json`](https://prices.curve.finance/v2/docs/openapi.json) — 6 endpoints | **Pools**: TVL, volume, APR, gauges |
| **Prices v1** | `https://prices.curve.finance/v1` | [`/feeds-docs/openapi.json`](https://prices.curve.finance/feeds-docs/openapi.json) — 136 endpoints | **Charts** (OHLC), crvUSD, lending, DAO |
| **Lite** | `https://api2.curve.finance` | [`/docs/openapi.json`](https://api2.curve.finance/docs/openapi.json) — 6 endpoints | **Curve Lite chains**: pools only |
| **Main v1** | `https://api.curve.finance/v1` | [`/v1/openapi.json`](https://api.curve.finance/v1/openapi.json) — 44 endpoints | superseded by v2; still the only source of zap addresses |

All public — no key, no auth.

---

## Prices API v2 — pool data

Six endpoints:

```
GET /v2/ping
GET /v2/pools/                              list, filtered + sorted + paged
GET /v2/pools/{chain_id}/{address}          one pool, in full
GET /v2/pools/chains/                       chain name <-> chain_id
GET /v2/pools/registries/                   registry contracts per chain
GET /v2/pools/{chain_id}/users/{user}/positions
```

### Why v2 over the v1 main API

v1 needed **two** calls joined by address — `getPools/big` had no volume and no
base APY, `getVolumes` had no pool metadata. v2 returns all of it in one object,
and adds `merkle_apr`, which v1 had no equivalent for. It is also ~4× smaller
(351 KB vs 1.3 MB for Ethereum) and sorts, searches and filters server-side.

### ⚠️ Gotchas

- **`pagination` is capped at 50.** Anything larger is a `422`. There is no
  "give me everything" call — 385 Ethereum pools is 8 requests. This is the
  single biggest constraint on how a list view can be built.
- **`gauges` has two different shapes.** The list endpoint returns objects
  (`[{"address": …, "is_killed": false}]`); the detail endpoint returns bare
  strings (`["0x…"]`). Handle both. Skip killed gauges — they accept deposits
  and pay nothing.
- **The spec's own description says the endpoints "are scaffolds and return not
  implemented responses".** They are not — every one returns real data. Treat
  it as a signal that v2 is young, not that it is broken.
- **12 chains, against v1's 21.** Missing: aurora, avalanche, celo, harmony,
  kava, mantle, moonbeam, x-layer, zkevm, zksync. Gained: taiko. If you need a
  chain outside the twelve, v1 is still the only source.
- **v2 has no OHLC at all** — charts stay on v1.
- Addressing is by numeric `chain_id`, not the chain *name* v1 used. Read
  `/v2/pools/chains/` rather than hardcoding.

### `GET /v2/pools/`

| Param | Notes |
|---|---|
| `chain_id` | numeric; from `/v2/pools/chains/` |
| `page`, `pagination` | 1-based; **`pagination` max 50**, default 20 |
| `sort_by` | `name` `base_daily_apr` `crv_apr` `crv_rewards_apr` `token_rewards_apr` `merkle_apr` `aggregate_apr` `volume` `tvl` |
| `sort_direction` | `asc` \| `desc` |
| `search_string` | matches pool and token names |
| `pool_type` | `main` `factory` `crypto` `crvusd` `factory_tricrypto` `stableswapng` |
| `min_tvl` / `max_tvl` | **`min_tvl=10000` takes Ethereum from 2210 pools to 385** and is what makes an APR sort useful — without it the top of an APR sort is all zero-TVL dust |
| `min_volume` / `max_volume`, `min_apy` / `max_apy`, `min_merkle_apr` / `max_merkle_apr`, `min_creation_date` / `max_creation_date` | |
| `user`, `include_blacklist` | |

Response: `{page, pagination, count, pools: [...]}`. `count` is the total
matching the filters, not the page size.

**`aggregate_apr` = base + CRV + token rewards + merkle.** Verified by
reproducing the ordering arithmetically. There is no rewards-without-base field,
so it is the closest server-side match for "sort by incentives".

### Pool object (list endpoint)

`chain_id`, `name`, `address`, `creation_date`, `vyper_version`, `pool_type`,
`is_metapool`, `base_pool`, `tvl_usd`, `trading_volume_24h`, `trading_fee_24h`,
`liquidity_volume_24h`, `liquidity_fee_24h`, `base_daily_apr`,
`base_weekly_apr`, `crv_apr`, `crv_apr_boosted`, `extra_rewards_apr[]`
(`{symbol, apr}`), `merkle_apr`, `gauges[]`, and `coins[]` with
`{pool_index, symbol, name, address, decimals}`.

`crv_apr` and `crv_apr_boosted` are the two ends of the veCRV boost range —
what Curve's own UI prints as "1.56% → 3.90% CRV".

### Detail endpoint adds

The list payload omits these entirely, so a pool page needs a second call:

`lp_token_address` (**required for withdraw and stake**), `registry_type`,
`n_coins`, `balances[]`, `balances_usd[]`, `coins[].usd_price`, `metadata`
(`a`, `fee`, `virtual_price`, `gamma`, `price_scale`, …), `info`
(`vyper_version`, `deployment_tx`, `deployment_block`), `asset_types`,
`oracles`, `lending_indices`.

Note `balances` are already scaled to human units, unlike v1's raw integers.

### Metapool underlying swaps

`exchange_underlying` / `get_dy_underlying` are on **StableSwap metapools**
(`int128` indices). Crypto metapools do not have them at all — their per-pool
zap does, with `uint256` indices, and it is then the spender. No factory zap of
either kind has any swap function; checked in the deployed bytecode of all
seven per-pool zaps and of the stable and crypto factory zaps.

### Pool type → ABI variant

`pool_type` / `registry_type` is the discriminator for which exchange ABI a pool
speaks. Getting it wrong does not revert — it returns empty data (see below).

| StableSwap (`int128` indices) | CryptoSwap (`uint256` indices) |
|---|---|
| `main`, `factory`, `crvusd`, `stableswapng` | `crypto`, `factory_crypto`, `factory_tricrypto`, `twocryptong` |

These names differ from v1's hyphenated registry ids (`factory-stable-ng`,
`factory-twocrypto`, …).

---

## Lite API — the small deployments

`api2.curve.finance`. A **Curve Lite** deployment is the factory contracts and
a gauge without the indexing the big chains have, so this API knows what the
pools know and nothing about trading. Six endpoints, FastAPI, and the spec is
at `/docs/openapi.json` (not `/openapi.json`, which 404s).

| Endpoint | Returns |
|---|---|
| `GET /get_platforms` | every deployment: `chain_id`, display name, RPC URL, explorer, TVL, `is_mainnet` |
| `GET /get_pools/{chain_id}` | **every pool on the chain in one response** — no paging, no filters |
| `GET /get_hidden_pools` | `(chain_id, address)` pairs Curve's own frontend does not show |
| `GET /get_deployment/{chain_id}` | factory addresses |
| `GET /health`, `/ping` | — |

24 platforms as of writing, 15 of them mainnets: Etherlink, Monad, Plasma,
X Layer, Unichain, Ink, Plume, Robinhood, Sonic, Stable, Tac, Taiko, XDC, plus
Avalanche and Fantom, which v2 also carries. Where both cover a chain, prefer
v2 — it has volume and charts.

### What is missing, which is the whole point

No volume, no base APR, no CRV boost range, and **no OHLC anywhere**, so no
chart. These are not zeroes to display: nothing measures them. Reward entries
carry `apy: null` wherever the token has no price, which is most of the time —
the emission `rate` beside it is not a substitute.

### Pool object

```
address, chain_id, registry_id, name, symbol, tvl, total_supply,
lp_token_address, virtual_price (1e18), amplification_coefficient,
is_meta_pool, is_broken, gauge_address, gauge_is_killed, gauge_crv_apy,
gauge_extra_rewards[], coins[{address, symbol, decimals, usd_price,
pool_balance, is_base_pool_lp_token}]
```

Three differences from a v2 pool that matter when parsing:

* `pool_balance` is a **raw integer** and `decimals` is a **string**; v2 sends
  reserves already scaled;
* `registry_id` uses underscores — `factory_stable_ng`, `factory_twocrypto`,
  `factory_tricrypto` — where v2 writes `stableswapng` and v1 writes
  `factory-stable-ng`. Same implementations, third spelling;
* a metapool's `coins` are the contract's own two, not decomposed. There is no
  base pool address at all, so the underlying route has nothing to address.

`is_broken` pools and the `get_hidden_pools` list are both worth filtering:
Curve's own frontend hides them.

---

## Prices API v1 — charts

v2 has none of these. The paths are **top-level, not nested under `/pools/`** —
`/v1/pools/{chain}/{addr}/ohlc` returns 404.

**`agg_units` accepts exactly `minute`, `hour`, `day`** (schema
`api__routes__v1__utils__Units`), combined with any `agg_number`. All nine
candle sizes this app offers were checked against `lp_ohlc` and come back at
exactly the requested spacing:

| picker | `agg_number` | `agg_units` |
|---|---|---|
| 15m / 30m | 15 / 30 | `minute` |
| 1h / 4h / 6h / 12h | 1 / 4 / 6 / 12 | `hour` |
| 1d / 7d / 14d | 1 / 7 / 14 | `day` |

⚠️ **The OHLC data contains occasional glitched wicks.** A daily candle for
`0x4f49…3c85` (Strategic USD Reserves, USDC/USDT) reports `low: 0.024289`
alongside `open/high/close` all at ~1.0158. Fit a price axis to raw min/max and
one such candle flattens the entire chart, so clamp against the candle bodies
rather than trusting the wicks.

```
GET /v1/lp_ohlc/{chain}/{address}
    required: start, end       optional: agg_number, agg_units, price_units
    -> the pool's LP token price; what Curve's pool page charts by default

GET /v1/ohlc/{chain}/{address}
    required: main_token, reference_token, start, end
    optional: agg_number, agg_units
    -> one coin priced in another, within a single pool

GET /v1/snapshots/{chain}/{address}
    required: start, end       optional: unit
    -> 27 fields incl. base_daily_apr, base_weekly_apr, virtual_price, fee

GET /v1/chains/{chain}         chain-wide totals under `total`
GET /v1/usd_price/{chain}/{address}[/history]
GET /v1/volume/{chain}/{address}
```

`main_token`/`reference_token` are **coin** addresses; the pool address only says
which market to read them from. `start`/`end` are Unix seconds. These use the
chain *name*, not the id.

`/v1/chains/{chain}` also returns every pool on a chain in one call (1298 on
Ethereum, 2.4 MB) — but its `page`/`per_page` params are broken (500).

---

## Transport notes

- **CORS**: every host sends `access-control-allow-origin: *`, so a browser
  build needs no proxy. Verified on all three.
- **⚠️ The main v1 API 403s the default `Python-urllib/*` User-Agent.**
  `requests`/`httpx`/`aiohttp` are unaffected. Only that literal string is
  blocked, but a stdlib fallback would hit it, so set an explicit UA.
- Cloudflare caches at `s-maxage=300`; polling faster returns the same bytes.
- v1 main wraps everything as `{success, data, generatedTimeMs}`. The Prices
  APIs do not — they return bare objects.
- `api.curve.fi` 301s to `api.curve.finance`.

---

## Main API v1 — for reference

Still the only source for chains v2 does not cover, and for gauges, lending
vaults and crvUSD supply.

- `GET /v1/getPools/big[/{chain}]` — pools ≥$10k TVL. Variants: `big`, `small`,
  `empty`, `all`; `/{chain}/{registryId}` for one registry.
  **No volume or base APY in it** — join `getVolumes/{chain}` on lowercased
  address (measured 382/382 on Ethereum).
- `GET /v1/getPlatforms` — 21 chains and their registries.
- `GET /v1/getVolumes/{chain}` — `volumeUSD`, `latestDailyApyPcent`,
  `latestWeeklyApyPcent`. (`getSubgraphData` returns the same APY as a raw
  fraction, not a percent.)
- `GET /v1/getAllGauges`, `/v1/getLendingVaults/all`, `/v1/getHiddenPools`,
  `/v1/getTokens/all/{chain}`, `/v1/getWeeklyFees`, `/v1/getGas`.

Deprecated in favour of `getPools`: `getFactoryV2Pools`, `getFactoryCryptoPools`,
`getFactoryTVL`, `getMainRegistryPools`, `getMainRegistryPoolsAndLpTokens`,
`getMainPoolsGaugeRewards`, `getFactoGauges`. `getFactoryAPYs` and
`getMainPoolsAPYs` are documented as *inaccurate*, and `getFactoryAPYs` needs a
`/{version}` segment or it 301s.

Registry ids (v1 spelling): `main`, `crypto`, `factory`, `factory-crypto`,
`factory-crvusd`, `factory-tricrypto`, `factory-twocrypto`,
`factory-stable-ng`, `factory-eywa`.

---

## Contract-level gotcha

Not an API issue, but it belongs with the pool-type mapping above:

> **Calling a function a Curve pool does not implement returns empty data, not a
> revert.** `decode_uint("0x")` is `0`, so a mis-typed pool quotes every swap at
> zero output instead of failing.

Confirmed against mainnet: a StableSwap-signature `get_dy` sent to tricrypto2
returns `0x`. Reject empty return data explicitly.

See [`examples/fetch_pools.py`](examples/fetch_pools.py) for a working,
dependency-free client for the v2 list endpoint.
