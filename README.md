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
.venv/bin/flet publish                # browser -> ./dist  (run from the repo root)
python -m http.server 8000 -d dist
```

The wallet layer is [`flet-pay-example`](https://github.com/michwill/flet-pay-example)'s
`wallet/` package, copied unchanged. Its README is the reference for how the
EIP-1193 seam and the browser bridge work; this one covers what was added on top.

---

## The shape of it

```
src/curve/     no Flet anywhere in it — pure logic, directly testable
    api.py         Prices v2 (pools) + v1 (charts), and the paging cursor
    models.py      Pool/Coin/Incentive, and parsing the API's shapes
    sort.py        column -> the v2 sort field it maps to
    format.py      numbers -> the strings a dense table can show
    abi.py         calldata for the pool contracts
    pool.py        those calls, bound to a connected wallet
    http.py        the browser/desktop fetch seam

src/ui/        Flet controls. Imports curve/, never the reverse.
    pool_list.py   search, sortable columns, scroll-driven paging
    pool_detail.py chart + composition + yields + actions
    actions.py     deposit / withdraw / swap / stake
    candles.py     the price chart: candles, axes, crosshair
    viewport.py    pan/zoom window and the pixel<->data mapping

src/wallet/    unchanged from flet-pay-example
```

The direction of that dependency is the whole point: `curve/` never imports Flet,
so sorting, formatting, ABI encoding and API parsing are testable with plain
pytest and no display, no Flutter, and no browser.

## Reading Curve

Public APIs, no keys. Pool data comes from the **Prices API v2**; charts from
**v1**, which is the only one with OHLC. The full survey is in
[`docs/curve-api.md`](docs/curve-api.md); the parts that shape the code:

- **v2 returns everything about a pool in one object** — TVL, volume, base APR,
  the CRV boost range, reward tokens and merkle rewards. The older main API split
  those across `getPools` and `getVolumes` and needed a join by address; v2 is
  also ~4× smaller (351 KB against 1.3 MB for Ethereum) and adds `merkle_apr`,
  which v1 had no equivalent for.
- **`pagination` is capped at 50**, and there is no "give me everything" call.
  That one limit is why the list is a paged cursor and why the ordering moved
  server-side — see below.
- **The list endpoint omits `lp_token_address`, the reserves and per-coin
  prices**, so opening a pool costs one more request. Without it there is nothing
  to withdraw or stake.
- **`gauges` has two shapes**: objects with a kill flag on the list endpoint,
  bare strings on the detail endpoint.
- v2 covers **12 chains against v1's 21**. All six offered here are in the twelve;
  anything outside needs v1.
- Every host sends `access-control-allow-origin: *`, so the browser build needs
  no proxy.
- **The v1 main API 403s the default `Python-urllib/*` User-Agent.**
  `requests`/`aiohttp` are unaffected, but the desktop transport here *is*
  urllib, so every request sets a name of its own.

### Paging, and why sorting is the server's job

The 50-row cap left two options: pull every page before painting anything (eight
requests for Ethereum), or page as the list scrolls. This does the second — the
first page appears after one request and the rest arrive as they are needed.

The consequence is that ordering cannot be done on the client, because a client
cannot sort a list it has not fully loaded. So changing the sort or the search
resets the cursor and asks the server again. That is a round trip per sort, and
in exchange the top of the list is always the true top rather than the top of
whatever happened to be in memory. Search is debounced and sent as
`search_string`, so it covers the whole chain rather than the rows on screen.

`curve/sort.py` maps each column to a v2 `sort_by` field. "Incentives" maps to
`aggregate_apr`, the API's combined base + CRV + tokens + merkle figure — there
is no rewards-without-base field, and the difference is immaterial when base APR
is low single digits and incentive APRs run to hundreds of percent.

## The chart

`flet-charts`' `CandlestickChart` draws the candles and axes. It has no pan,
no zoom and no crosshair, so those are added around it the way the pyqtgraph
dashboard this is modelled on does:

```
GestureDetector        drag to pan, wheel to zoom, hover for the crosshair
  Stack
    CandlestickChart   the candles and axes
    Canvas             a transparent overlay — crosshair only
```

That split is the point. The chart never repaints for a mouse move; only the
overlay does. And the overlay *is* a canvas, because a crosshair is two lines
and a label — which is what canvas is for, unlike the candles, which were not.

- **Drag** pans both axes. Content follows the cursor, and dragging down raises
  prices, because screen y grows downward and a chart that inverts that is
  unreadable.
- **Wheel** zooms in time, anchored on the candle under the pointer so it stays
  under the pointer. Price is panned, not zoomed — same as the Qt version.
- **Hover** draws the crosshair: dashed lines through the cursor, the price
  boxed against the left axis, the timestamp boxed on the date axis, and the
  hovered candle's OHLC in the top-left corner rather than trailing the pointer.
  Hover is throttled to 25/s; every event is a round trip into Python, and the
  Qt version throttles for the same reason.
- **Double-tap** refits the whole series.

No animation on any of it. A 250ms ease flatters a data swap and is actively
wrong under direct manipulation: every drag frame sets a new window, so the
chart spends its time easing towards where the cursor *was*. It felt like
dragging through treacle until the animation came out.

Only the visible window (plus a small margin) is sent to the chart, so a drag
at 1Y serialises ~20 spots rather than 365.

**A candle thinner than a pixel is drawn as nothing.** Not a hairline, not a
dot — a gap. On a stable pool that is most of them: Strategic USD Reserves over
7 days has **101 of its 169 hourly candles under one pixel** of high-to-low, so
the chart looked like it was missing data when the data was complete and
gapless. Every candle handed to the chart is therefore floored to
`MIN_CANDLE_PX` of extent, widened about its own midpoint. The floor applies
only to the chart's copy — `_candles` keeps the true values, so the crosshair
still reads out real numbers.

**Known limitation: candle width does not scale with zoom.**
`CandlestickChart` draws candles at a fixed pixel width — there is no width
property on the chart or on `CandlestickChartSpot`, and the rendered width is
unchanged whether it is handed 365 spots across 90 days or 20 across 17. So
zooming in spreads the candles apart instead of fattening them. Fixing it
properly means painting the bodies onto the overlay canvas and leaving the
control to draw only the axes and grid — a real option, since the overlay and
the pixel mapping already exist, but it gives up the control for the part it
was chosen for.

The window is clamped to keep the data on screen, with half a window of
overscroll at each end — stopping dead on the last candle reads as the chart
being stuck — and never narrower than five candles, or one scroll burst zooms
until the chart is a single bar.

`viewport.py` holds all of that arithmetic, which makes the awkward cases —
zooming past the ends, panning off the data, a plot box too small to divide by
— cheap to test without a mouse. One honest limitation: `CandlestickChart` does
not expose where it drew its plot area, so `Plot` reconstructs it from the axis
label sizes this app itself sets. The crosshair readout is therefore accurate to
a pixel or two rather than exact.

### Axis gotchas

Both found by looking at the rendered result, and neither is obvious:

- Axis labels must land on **multiples of `label_spacing`**. The chart ticks at
  multiples of the interval counted from zero, so labels at `min + i*step` are
  dropped and the axis shows only its endpoints. `nice_interval` picks a round
  step (1, 2, 2.5 or 5 × 10ⁿ) and labels go on multiples of it. The date axis
  never hit this — its values are integer multiples of the stride already.
- Derive label precision from the **interval**, not the span, or an axis
  stepping by 0.0001 prints "1.026800" where "1.0268" is the number.

Charts are also the one place `flet publish` bit: it resolves dependencies
relative to the **script's** directory, so `flet publish src/main.py` never saw
the root `pyproject.toml`, fell back to bare `flet`, and the published app died
in Pyodide with `ModuleNotFoundError: No module named 'flet_charts'`. Publish
from the project root instead — `flet publish`, no path.

## Talking to the pools

`curve/abi.py` computes selectors with keccak-256 instead of hard-coding them,
which is the opposite of what `wallet/erc20.py` does — and deliberate. ERC-20 has
five fixed signatures; Curve's depend on the pool:

| | StableSwap | CryptoSwap |
|---|---|---|
| `pool_type` (v2) | `main`, `factory`, `crvusd`, `stableswapng` | `crypto`, `factory_crypto`, `factory_tricrypto`, `twocryptong` |
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

## Testing

Three layers, and the first two need nothing at all:

```bash
.venv/bin/python -m pytest tests/ -q                      # 154 tests, ~0.9s
.venv/bin/python -m pytest tests/integration -m flet_ui   # 6 tests, ~25s each
```

**1. Logic tests.** `curve/` imports no Flet, so its ABI encoding, parsing,
sorting and formatting test as ordinary Python. `test_pool.py` drives
`PoolContract` against a fake EIP-1193 provider that records calldata and answers
from a script, covering every transaction without a wallet. `test_feed.py` covers
the paging cursor, including the race where a page arrives after the query
changed and must be discarded rather than appended to a different sort.

**2. Constructor tests.** `test_views.py` builds every view against a stub page.
Flet validates control arguments in `__init__`, so this catches the whole class of
"wrong keyword for this Flet version" bug in half a second — it found
`on_scroll_interval` (it is `scroll_interval`) and `ft.Tab(content=…)` immediately.
What it cannot see is layout, hit-testing or paint.

**3. UI tests — `flet.testing`.** Seven tests that start the real app and drive it:
`find_by_key`, `tap`, `enter_text`, `pump_and_settle`, screenshots. They are
marked `flet_ui` and excluded from the default run.

> An earlier version of this README claimed these needed a pre-installed Flutter
> SDK. **That was wrong.** `flet-cli` downloads and provisions Flutter itself (to
> `~/flutter/`) on first use. Budget ~5 minutes for that first run; warm runs are
> ~25s per test.

They earn their keep: every bug in this project so far has lived in exactly the
layer only they can reach.

- A `TextButton` column heading that hovered correctly but never fired
  `on_click` in a published build — no exception, the event simply never arrived.
- `ft.Tab(content=…)`, which blew up only when a pool page was opened.
- **`ft.TabBarView(height=520)` raised a widget exception in a Flutter debug
  build.** The browser release build rendered it perfectly, so nothing but this
  suite was ever going to catch it. Sizing the view by `expand` inside an
  expanded `Column` fixes it. Bisecting to that took a while — the first two
  suspects (the composition `DataTable`, the fixed-width action panel) were both
  wrong, though replacing the table with plain Rows did fix a real `$1.`
  formatting bug on the way past.

Two limits worth knowing before adding to `tests/integration/`:

- In device mode (the default) `flet_app.tester` is a `RemoteTester`, whose API
  is a **subset** of `Tester` — `find`/`tap`/`enter_text`/`take_screenshot` are
  there, **`drag` is not**. So scrolling cannot be driven from a UI test, and the
  scroll-triggered paging is covered by a unit test plus a manual browser pass
  instead.
- `pump_and_settle` returns when animations stop, which is long before a network
  call answers. Every wait in that file is a polling loop.

`tests/integration/conftest.py` raises `FletTestApp`'s output cap, because the
default 256 KB keeps only a tail of the Flutter process's very chatty debug log —
and the exception that actually failed the test scrolls off it, leaving nothing
but `Test failed. See exception logs above.` Set `FLET_TEST_VERBOSE=1` to stream
it live instead.

## Flet 0.86 notes

Things that cost a debugging round here, beyond the ones already in
flet-pay-example's README:

- **Charts are not in core — they are in `flet-charts`.** An earlier version of
  this app drew candles by hand on `flet.canvas` because core has no chart
  controls and I did not check for a separate package. `flet-charts` is official,
  released on the same version line, and has `CandlestickChart` among others.
  It is pure Python — the Dart side ships with the standard client — so
  `flet publish` still needs no Flutter build. See the chart section above.
- **`ChartAxis` labels must sit on multiples of `label_spacing`.** The chart ticks
  at multiples of the interval counted from zero and only renders a label whose
  value lands on a tick; labels at `min + i*step` are silently dropped, leaving
  an axis showing just its min and max.
- **`GestureDetector` is how you add pan/zoom/hover to anything.** `on_pan_update`
  carries `local_delta`, `on_scroll` carries `scroll_delta` plus `local_position`,
  and `on_hover`/`on_exit` carry `local_position` — enough to drive a viewport
  without the wrapped control knowing. `drag_interval`/`hover_interval` throttle
  at the Dart end, and it is worth throttling again in Python.
- **A canvas overlay does not need to be hit-testable.** Stack the crosshair
  canvas *over* the chart and wrap the whole stack in one `GestureDetector`:
  the overlay never intercepts a drag, and the chart never repaints on a
  mouse move.
- **`Tabs` was restructured.** `ft.Tab` no longer takes `content` — it is only the
  button. The container is `Tabs(length=N, content=…)` holding a `TabBar` of
  `Tab`s and a `TabBarView` of bodies, and `length` must match both. Size the
  `TabBarView` with `expand`, not a fixed `height`: a fixed height raises a
  widget exception in a Flutter debug build while rendering fine on web.
- **`ListView` uses `scroll_interval`, not `on_scroll_interval`**, and it
  virtualises — only the rows currently on screen exist in the widget tree, so
  `find_by_key` cannot see a row that has scrolled out of view.
- **`Container` has no `scroll`**; wrap the child in a `Column` that does.
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
- `page.run_task` wants a coroutine *function* plus its arguments; hand it a bare
  coroutine object and it raises `TypeError`.
- `DataTable` sizes itself from its content and will not shrink, so it overflows
  a narrow parent. Plain `Row`s with fixed-width `Container` cells flex down.
- **`flet publish <path>` resolves dependencies relative to that path's
  directory**, not the repo root — so it silently ignored the root
  `pyproject.toml`, shipped a requirements list of just `flet`, and the app
  failed in the browser with `ModuleNotFoundError`. Run `flet publish` from the
  project root with no path; it reads `[tool.flet.app] path` and writes `./dist`.

## What was verified

Not claims — these were run:

- **The API client against the live endpoints**: 385 Ethereum pools over the TVL
  floor, all four server-side sorts, paging to 150 rows, search, the detail
  endpoint, chain totals, and both candle series (`lp_ohlc` and `ohlc`). Numbers
  match Curve's own UI — 3pool at $160.0m TVL, crv2pool's CRV range at
  1.56%→3.90% against Curve's 1.57%→3.91%.
- **The calldata against a mainnet node**, before it was wired to anything:
  `get_dy` (both index widths), `calc_token_amount` (both spellings) and
  `calc_withdraw_one_coin`, plus the confirmation that a wrong-width `get_dy`
  returns `0x` rather than reverting.
- **All twelve function selectors** against the values deployed on 3pool and
  tricrypto2.
- **The browser build driven in Chrome**: Pyodide boots, `pyfetch` reaches both
  APIs, the list renders and reorders, scrolling to the end pulled pages 2-5
  ("250 of 385 pools"), a pool opens onto a live candlestick chart, the
  timeframe buttons re-fetch, and all four action tabs render with their
  controls correctly disabled while no wallet is connected.
- **The chart, driven in Chrome** from the published build: wheel-zoom narrowed
  the window from May–August to 31 May–20 June anchored on the cursor, dragging
  panned both time and price, and the crosshair read out `1.0250`, `05 Jun
  00:00` and `O 1.0261 H 1.0262 L 1.0140 C 1.0254` for the candle under it.
  Axis labels correct at every scale; switching timeframe re-fetches and
  re-draws.
- **154 unit tests** with no display and no Flutter, plus **7 UI tests** driving
  the real app through Flet's own framework.

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
