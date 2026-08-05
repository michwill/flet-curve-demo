# Curve — Flet

An alternative Curve Finance UI, written once in Python and running both as a
**static website** (MetaMask/Rabby/WalletConnect) and as a **native desktop app**
(Frame/qeth over localhost JSON-RPC).

It lists every pool on a chain, sorts by volume, TVL, incentives or base APY,
and opens each pool onto a candlestick chart plus a panel that can deposit,
withdraw, swap and stake — all in-pool, no router.

```bash
uv venv && uv pip install -r requirements.txt

.venv/bin/flet run src/main.py        # desktop -> Frame / qeth on 127.0.0.1:1248
.venv/bin/flet publish src/main.py    # browser -> ./src/dist
python -m http.server 8000 -d src/dist
```

The wallet layer is [`flet-pay-example`](https://github.com/michwill/flet-pay-example)'s
`wallet/` package, copied unchanged. Its README is the reference for how the
EIP-1193 seam and the browser bridge work; this one covers what was added on top.

---

## The shape of it

```
src/curve/     no Flet anywhere in it — pure logic, directly testable
    api.py         the two Curve APIs, with a 5-minute cache
    models.py      Pool/Coin/Incentive, and parsing the API's shapes
    sort.py        the list's ordering and search rules
    format.py      numbers -> the strings a dense table can show
    abi.py         calldata for the pool contracts
    pool.py        those calls, bound to a connected wallet
    http.py        the browser/desktop fetch seam

src/ui/        Flet controls. Imports curve/, never the reverse.
    pool_list.py   search, sortable columns, virtualised rows
    pool_detail.py chart + composition + yields + actions
    actions.py     deposit / withdraw / swap / stake
    candles.py     a candlestick chart drawn on flet.canvas

src/wallet/    unchanged from flet-pay-example
```

The direction of that dependency is the whole point: `curve/` never imports Flet,
so sorting, formatting, ABI encoding and API parsing are testable with plain
pytest and no display, no Flutter, and no browser.

## Reading Curve

Two public APIs, no keys. The full survey is in
[`docs/curve-api.md`](docs/curve-api.md); the parts that shape the code:

- **`getPools` carries no volume and no base APY** — only gauge CRV APR. The list
  view is `getPools/big/{chain}` joined to `getVolumes/{chain}` on lowercased
  address. Measured 382/382 on Ethereum.
- **`big` rather than `all`**: 794 KB versus 4.7 MB, and the difference is all
  dead pools.
- Charts come from the *other* host, `prices.curve.finance`, whose time-series
  paths are top-level (`/v1/lp_ohlc/…`, `/v1/ohlc/…`) rather than under `/pools/`.
- Both hosts send `access-control-allow-origin: *`, so the browser build needs no
  proxy.
- **The API 403s the default `Python-urllib/*` User-Agent.** `requests`/`aiohttp`
  are unaffected, but the desktop transport here *is* urllib, so every request
  sets a name of its own.

## Talking to the pools

`curve/abi.py` computes selectors with keccak-256 instead of hard-coding them,
which is the opposite of what `wallet/erc20.py` does — and deliberate. ERC-20 has
five fixed signatures; Curve's depend on the pool:

| | StableSwap | CryptoSwap |
|---|---|---|
| registries | `main`, `factory`, `factory-crvusd`, `factory-stable-ng` | `crypto`, `factory-crypto`, `factory-twocrypto`, `factory-tricrypto` |
| coin index type | `int128` | `uint256` |
| `exchange` selector | `0x3df02124` | different |

`add_liquidity` takes a *fixed-size* array, so a 2-coin and a 3-coin pool are
different functions again. That is dozens of variants, and the failure mode is
not loud:

> **Calling a function a Curve pool does not implement returns empty data, not a
> revert.** `decode_uint("0x")` is `0`, so a mis-typed pool would quote every swap
> at zero output rather than failing.

Confirmed against mainnet: a StableSwap-signature `get_dy` sent to tricrypto2
returns `0x`. Every read in `curve/pool.py` therefore rejects empty return data,
and a real 32-byte zero is still accepted as the legitimate answer it is.

The encoding was verified against a live node before any of it was wired to a
button — `calc_token_amount` for 1000 DAI into 3pool returned 962.16 LP, which is
exactly 1000 ÷ the pool's 1.0398 virtual price.

Two smaller decisions worth stating: approvals are for the **exact amount**, not
`MAX_UINT256`; and gas and nonce are never set, because every wallet in scope
fills them in and knows the chain better than this app does.

## Off-screen testing in Flet

Short answer: **yes for the logic, and yes for building the control tree — but
Flet's own UI-driving tests need the Flutter SDK.**

Flet 0.86 ships a real integration-testing framework: `flet/testing/`, a
`flet_app` pytest fixture, and a `Tester` with `find_by_key`, `tap`,
`enter_text`, `pump_and_settle` and screenshot comparison. It drives a real app.
Both of its modes need a Flutter *test host* directory — device mode provisions
one via `flet-cli` (needs the Flutter SDK), host mode wants
`FLET_TEST_FLUTTER_APP_DIR` pointing at one already built. There is no
in-process, SDK-free renderer.

So this project takes the two layers that need nothing:

```bash
.venv/bin/python -m pytest tests/ -q      # 105 tests, ~0.4s, no display
```

1. **Logic tests** — `curve/` imports no Flet, so its ABI encoding, parsing,
   sorting, searching and formatting test as ordinary Python. `test_pool.py`
   drives `PoolContract` against a fake EIP-1193 provider that records calldata
   and answers from a script, which covers the transactions without a wallet.
2. **Constructor tests** — `test_views.py` builds every view with a stub page.
   Flet validates control arguments in `__init__`, so this catches the entire
   class of "wrong keyword for this Flet version" bug in 0.4 seconds. It found
   two real ones (below) that otherwise only appeared on the second click of a
   published build.

What that does *not* cover is layout, hit-testing and paint — and one bug below
lived exactly there, so a browser pass is still worth doing before shipping.

## Flet 0.86 notes

Things that cost a debugging round here, beyond the ones already in
flet-pay-example's README:

- **There are no chart controls in core at all.** No line, bar or pie, let alone
  candlestick. `flet.canvas` (`Rect`, `Line`, `Text`, `Paint`) is the answer, and
  it is a good one — a candle is two rectangles and a line. The canvas learns its
  size only at layout time, so shapes are built in `on_resize` and the geometry is
  a pure function of `(candles, width, height)`.
- **`Tabs` was restructured.** `ft.Tab` no longer takes `content` — it is only the
  button. The container is `Tabs(length=N, content=…)` holding a `TabBar` of
  `Tab`s and a `TabBarView` of bodies, and `length` must match both.
- **`control.page` raises `RuntimeError` when the control is not mounted**, rather
  than returning `None`. So `if self.page: self.update()` raises the very error it
  looks like it is guarding. There is no public `is_mounted`; `ui.safe_update()`
  attempts the update and swallows that one case.
- **Subclassing a control means `page` is taken.** `ft.Column` defines it as a
  read-only property, so a view that stores its own `self.page = page` dies with
  "property 'page' of … has no setter".
- **A `TextButton` in the table header hovered but never fired `on_click`** in the
  published web build — no exception, the event simply never arrived, while the
  identical handler on a `Container` in the same list worked. The column headings
  are `Container(on_click=…, ink=True)` for that reason; the full-cell hit target
  is better anyway.
- **The web build's font has no arrow glyphs.** `→` and `↓` render as tofu boxes
  in the browser and fine on desktop. User-visible strings stay ASCII (`·` and `–`
  are verified exceptions) and the sort arrow is a Material icon.
  `test_user_visible_strings_stay_within_ascii` pins it.
- Axis labels need precision derived from the **span**, not the magnitude: a
  stable pool ranging over 1.0268–1.0271 prints "1.027" three times at four
  decimals.
- `SegmentedButton.selected` is a `list[str]`, not a set.
- `flet publish src/main.py` writes to **`src/dist`**, not `./dist`.

## What was verified

Not claims — these were run:

- **The API client against the live endpoints**: 382 Ethereum pools loaded and
  joined, all four sorts, search, chain totals, and both candle series
  (`lp_ohlc` and `ohlc`). Numbers match Curve's own UI — 3pool at $159.98m TVL,
  crv2pool's CRV range at 1.57%→3.92% against Curve's 1.57%→3.91%.
- **The calldata against a mainnet node**, before it was wired to anything:
  `get_dy` (both index widths), `calc_token_amount` (both spellings) and
  `calc_withdraw_one_coin`, plus the confirmation that a wrong-width `get_dy`
  returns `0x` rather than reverting.
- **All twelve function selectors** against the values deployed on 3pool and
  tricrypto2.
- **The browser build driven in Chrome**: Pyodide boots, `pyfetch` reaches both
  APIs, the pool list renders 382 rows, sorting by incentives and by TVL both
  reorder correctly, a pool opens onto a live candlestick chart, the timeframe
  buttons re-fetch, and the Deposit/Withdraw/Swap/Stake tabs all render with
  their controls correctly disabled while no wallet is connected.
- **105 unit tests**, no display and no Flutter.

Not yet exercised: a signed transaction. The read path, the encoding and the
approve/submit gating are all verified, but nothing here has been broadcast from
a funded account — deposit, withdraw, swap and stake have not been run end to
end against a real balance.

## Deliberately not built

- **No router.** Swaps are `exchange` within the one pool, as asked. Cross-pool
  routing is a different problem and a much larger one.
- **No balanced-deposit helper, no zaps, no metapool underlying deposits.**
  Deposits go to the pool's own `add_liquidity` with explicit per-coin amounts.
- **Withdrawal floors on the balanced path** are derived from the reserves the API
  reports rather than from `calc_token_amount(…, is_deposit=False)`. Sending zero
  floors would be simpler and is what many UIs do; it also offers no protection
  against a sandwich.
- **No claim-rewards button.** Staking and unstaking are there; `mint`/
  `claim_rewards` are not.
