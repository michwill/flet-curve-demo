# Curve — Flet

An alternative Curve Finance UI, written once in Python and running both as a
**static website** (MetaMask/Rabby/WalletConnect) and as a **native desktop app**
(Frame/qeth over localhost JSON-RPC).

It lists every pool on a chain, sorts by volume, TVL, incentives or base APY,
and opens each pool onto a candlestick chart plus a panel that can deposit,
withdraw, swap and stake — all in-pool, no router.

```bash
git submodule update --init          # curve-assets: logos and token images
uv venv && uv pip install -r requirements.txt
python tools/build_assets.py         # compile the subset the app needs
python tools/build_icons.py          # only when the mark changes: app icon + favicon

.venv/bin/flet run src/main.py        # desktop -> Frame / qeth on 127.0.0.1:1248
.venv/bin/flet publish                # browser -> ./dist  (run from the repo root)
python tools/serve.py                # serve ./dist with caching off
```

To offer **WalletConnect** in the browser build, give it a projectId (free,
from [dashboard.reown.com](https://dashboard.reown.com)):

```sh
cp src/local_config.example.toml src/local_config.toml   # then fill in project_id
```

That file is gitignored and read at build time -- `flet publish` bundles it, so
there is no post-build step to forget. Without it the WalletConnect connector
simply does not appear; injected wallets (MetaMask, Rabby, Frame, qeth) work
either way.

The wallet layer is [`flet-pay-example`](https://github.com/michwill/flet-pay-example)'s
`wallet/` package. Its README is the reference for how the EIP-1193 seam and the
browser bridge work; this one covers what was added on top. Four changes were
made to it here:

- **the desktop transport polls.** An HTTP endpoint cannot push, so switching
  account in Frame or qeth used to change nothing on screen. `desktop.py` now
  asks every four seconds and synthesises the same `accountsChanged` /
  `chainChanged` events a browser wallet sends -- `Wallet` cannot tell which
  transport it is on.
- **`disconnect` counts as a disconnection.** An extension revokes a site with
  an empty `accountsChanged`; WalletConnect closes the session and sends
  `disconnect`. Only the first was handled.
- **disconnecting is remembered.** A desktop build connects at startup by
  design -- a local wallet raises no popup -- which meant relaunching undid
  a deliberate disconnect. `wallet/consent.py` leaves a marker; connecting
  removes it.
- **the connection survives the tab closing.** The bridge remembers which
  wallet was used (by `rdns`, since an EIP-6963 uuid is regenerated per page
  load) and `Wallet.restore()` picks it up on the next load -- asking
  `eth_accounts`, never `eth_requestAccounts`, so a page that opens by
  itself never opens a wallet dialog by itself. Disconnecting forgets it.
- **one bridge serves an origin.** A `BroadcastChannel` reaches every tab, so
  two tabs of this app meant two bridges answering one request -- and the
  faster one won, which is how a picker ended up listing another tab's
  wallets and a selection landed somewhere the account request did not.
  `wallet_bridge.js` now takes a Web Lock and only the holder answers;
  replies and wallet events are addressed to the client that asked. The
  browser passes the lock on when that tab closes.

Serve the build with `tools/serve.py` rather than `python -m http.server`:
the latter sends no cache headers, Chrome caches heuristically, and a reload
happily keeps running the *previous* `app.tar.gz`. That is worth a script
because the failure looks exactly like a fix that did not work.

## Publishing it to IPFS

The whole app is static, so it pins as a directory. `tools/publish_ipfs.py`
publishes, makes the build portable, and uploads it to Pinata:

```sh
python tools/publish_ipfs.py               # flet publish, then pin ./dist
python tools/publish_ipfs.py --no-build    # pin the dist/ already there
python tools/publish_ipfs.py --dry-run     # everything up to the upload
```

It builds with `--app-short-name`, which a plain `flet publish` does not:
the manifest's short name otherwise falls back to the *project's* name, so
an installed shortcut is labelled "flet-curve" on an Android home screen.

**The key does not go in `src/local_config.toml`.** That file is under `src/`
so that `flet publish` bundles it -- which is exactly why a real credential
cannot live there: it would be served to every visitor, and pinning it puts
it somewhere with no unpublish. It says as much in its own header. Put the
Pinata JWT at the repo root instead, or in the environment:

```sh
cp local_secrets.example.toml local_secrets.toml   # gitignored; then fill in jwt
export PINATA_JWT='...'                            # or this, which wins
```

The script greps the build for whatever key it is about to authenticate with
and refuses to upload if it finds it -- in a loose file or inside
`app.tar.gz`, which is where `src/local_config.toml` ends up. The difference
between the safe file and the unsafe one is one path component and the
mistake cannot be taken back, so it is checked rather than documented.

Two more things it does, both because a gateway serves a site under a
sub-path (`/ipfs/<cid>/`) rather than at a root:

- **`<base href="/">` becomes `<base href="./">`.** Otherwise the bootstrap,
  the wasm, canvaskit and `app.tar.gz` are all fetched from the gateway's
  root, where none of them are: a blank page and a pile of 404s. Relative is
  identical at a real root, so the local server is unaffected. The app's own
  asset URLs need nothing -- `ui/assets.py` builds them from the worker's
  `location`, which is already the right prefix.
- **it asks for a CIDv1.** Base32 and case-insensitive, so it fits in a
  hostname and `https://<cid>.ipfs.dweb.link/` works; a v0 hash is base58 and
  only resolves on a path gateway.

**Pinata will not serve the result over its own gateways.** Both refuse HTML
with `ERR_ID:00023` -- the public one says to use a dedicated gateway, and
the dedicated one says to put a custom domain on it. Everything that is not
HTML (the js, the wasm, `app.tar.gz`, the images) is served happily by both,
so the failure looks like a broken pin rather than a policy. Three things
that do work:

- any third-party gateway: `https://<cid>.ipfs.dweb.link/`;
- a custom domain attached to the dedicated gateway;
- an ENS name whose contenthash is `ipfs://<cid>`, through `eth.limo` -- the
  CID is v1 for this reason among others.

Pinata's current upload API (`uploads.pinata.cloud/v3/files`) takes one file
and not a directory, and a website is a directory, so this posts to the older
`pinFileToIPFS` -- which is what their own docs point to for folders. One
request, one `file` part per file, each named `<folder>/<path>`; that naming
is what rebuilds the tree on the other side, and why the CID that comes back
is a directory you can open.

The body is built by hand rather than handed to `httpx` as 1,800 open file
handles, which hits the descriptor limit long before the network. It streams
one file at a time with the length computed up front, so the request is not
chunked -- and `tests/test_ipfs.py` asserts the computed length is exactly
what the generator emits, because a body that disagrees with its own
`Content-Length` either truncates at 100 MB or hangs.

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

## Talking to the pools

Curve is not one ABI. Two axes vary independently, and both were checked
against mainnet rather than inferred from the registry names:

| implementation | coin index | amount arrays |
| --- | --- | --- |
| `main`, `crvusd`, `factory` (old stable) | `int128` | `uint256[N]` |
| `stableswapng` | `int128` | **`uint256[]`** |
| `crypto`, `factory_tricrypto`, `twocryptong` | `uint256` | `uint256[N]` |

StableSwap-NG rewrote its amounts as a Vyper `DynArray`, so `add_liquidity`,
`remove_liquidity` and `calc_token_amount` take an offset and a length where
every other pool takes N inline words. A fixed-array call to one of those
pools reverts -- which is what "could not read the deposit estimate" was.

The registry says which to expect and `PoolContract` asks anyway: the quote
tries both spellings, and the one that answers is remembered on the pool,
because a transaction gets no second attempt. An unknown future factory is
therefore handled by asking rather than by guessing.

The estimates are not equally trustworthy, either, so the slippage the app
fills in is `fee + 0.005%` to deposit or withdraw and `0.2 * fee` to swap.
`get_dy` is exact -- the arithmetic the swap itself runs -- while
`calc_token_amount` is computed fee-free on the old StableSwap pools, so a
`min_mint` built from it reverts. Both constants were measured: deposits run
on a titanoboa fork across 44 pools, bisecting `min_mint` until the
transaction stopped reverting, and quote staleness against the archive.
[`docs/slippage.md`](docs/slippage.md) has the tables, the two measurement
traps that produced wrong answers first, and why the fee term is what lets
the constant stay at 0.005%.

### Metapools

A metapool holds two coins: its own, and the base pool's LP token. Almost
nobody holds that LP token — what they have is USDC — so Deposit, Withdraw and
Swap each offer a **Pool tokens / Underlying** switch, defaulting to underlying.

Swapping is the easy half — on a StableSwap metapool. `exchange_underlying` is
on the pool itself, which does the base-pool leg internally, so it approves the
pool like any other swap and works on every chain, including the ones where no
zap was ever deployed.

A *crypto* metapool has no such function at all. Its **per-pool zap** does:
all seven carry `exchange_underlying` and `get_dy_underlying`, checked in their
deployed bytecode, so the route exists there too and the zap is what gets
approved. The factory zaps of either kind have no swap functions, so a crypto
metapool served only by one of those has no underlying swap — and says so
rather than sending calldata nothing implements.

Depositing and withdrawing need a zap, and `curve/zaps.py` is mostly a table of
them, because there turned out to be **four dialects** and none is inferable
from the others:

| | pool argument | array | `is_deposit` flag | indices |
|---|---|---|---|---|
| StableSwap-NG factory | yes | `uint256[]` | yes | `int128` |
| stable factory | yes | `uint256[N]` | yes | `int128` |
| crypto factory | yes | `uint256[N]` | **no** | `uint256` |
| per-pool crypto | **no** | `uint256[N]` | no | `uint256` |

The last row is the older crypto metapools, which each got a zap of their own,
so its calldata is exactly what the pool itself would take — just sent
elsewhere. That is also why there are three tables rather than one: what
*addresses* a zap differs, not only what it speaks.

The addresses come from `zapAddress` in the v1 main API, swept across all 21
chains and every registry, and each was then checked with an `eth_call` to that
zap on its own chain, quoting a real deposit. The Gnosis gap that prompted this
was a sweep bug: Curve's API calls that chain `xdai`, and asking for `gnosis`
quietly returned nothing.

**Quotes work with no wallet.** Nothing about `get_dy` or `calc_token_amount`
needs an account, so with nothing connected the panels read through public
endpoints instead — rates and the fee-derived slippage appear before anyone
connects anything, and only the buttons that move tokens stay off.

The endpoints come from [chainlist.org](https://chainlist.org)'s `rpcs.json`,
which is CORS-open so the browser build can read it too. It is a *list* because
they fall over: a request walks it until one answers, and the survivor is where
the next read starts, so a dead host at the top is paid for once rather than on
every keystroke. Entries this app cannot call are dropped up front — websockets,
API-key templates, plain `http://` — and endpoints that report keeping no logs
are tried first. A JSON-RPC *error* is not retried: a reverted `eth_call` is an
answer, and asking somebody else gets the same one.

The file is a couple of megabytes and there is no per-chain endpoint, so it is
fetched once, lazily, on the first read that needs it — a session with a wallet
connected never asks for it at all. Which is the other half of the rule:

**a connected wallet is always preferred.** It is the node that will execute the
transaction, so a quote read through it is the quote least likely to surprise.
And reads go through the wallet's provider, so they land on whatever network the
*wallet* is on. Browsing Gnosis with a wallet on Ethereum quotes Gnosis
addresses against Ethereum, where they hold no code: every estimate comes back
"the pool did not answer", which reads as a pool this app cannot handle.

So picking a network in the header now takes the wallet with it —
`wallet_switchEthereumChain`, which the wallet prompts for and may refuse. A
wallet that has never heard of the network answers 4902; for a Curve Lite chain
that is the normal case, and `get_platforms` is the only place publishing the
RPC, explorer and native symbol that `wallet_addEthereumChain` needs, so the
offer is made with that. Only on a deliberate pick, never on load, where the
wallet's own network is a choice the app follows rather than overrides.

When they still disagree — a refusal, or a wallet moved by hand — each action
panel says which network to be on and offers the switch, with its estimate
cleared and its buttons greyed, since nothing can be read or sent across that
boundary.

A zap that will not answer costs nothing, since the route is gated on a working
quote — no approve step appears and the pool-token route stays. One family is
still unsupported for deposits: the `main`-registry metapools (GUSD/3Crv and
friends), whose per-pool deposit contracts answered none of the spellings
probed. Their underlying coins can still be swapped.

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

### Curve Lite, and showing less on purpose

A **Curve Lite** deployment is the factory contracts and a gauge without the
indexing the big chains have — fifteen mainnets, from Etherlink and Monad down
to pools worth a few hundred dollars — and it is served by a different API,
`api2.curve.finance`. They are in the same chain picker; `curve/lite.py` has
the client and `CurveApi` dispatches on chain id, so `PoolFeed` and the list
view never learn which kind of chain they are showing.

What that API does not have is the interesting part. **No volume, no base APR,
no CRV boost range, and no OHLC at all.** Those are not zeroes — nothing
measures them — so rather than print noughts that read as measurements:

- the Volume and Base APY **columns come out** of the table, and their sort
  options with them. The list opens on TVL, since sorting by a volume that is
  unknown everywhere orders the page arbitrarily;
- the chain header drops its volume clause, and so does the pool page;
- the chart is replaced by one line saying why there isn't one;
- a reward whose token has no price contributes nothing, rather than an APR
  guessed from the emission rate.

Three shape differences are handled in `Pool.from_lite`: raw integer balances
with string decimals (v2 sends them scaled), a third spelling of the registry
ids (`factory_stable_ng` where v2 says `stableswapng` — folded by
`Pool.registry_key`, and getting it wrong would send fixed-array calldata to a
`DynArray` pool), and metapool coins that are *not* decomposed, which is also
why no zap route is offered there.

Two other things this API decides rather than the app: pools marked `is_broken`
and the `get_hidden_pools` list are both dropped, as Curve's own frontend drops
them. And the TVL floor is zero here where the big chains use $10,000 — whole
Lite deployments are smaller than that floor, so the same cut would empty the
list. Sorting by TVL is what keeps the dust at the bottom.

Paging is local: one request returns the whole chain, and `curve/lite.py`'s
`select` filters, orders and slices it to the same `(page, total)` contract the
server gives for v2.

### The address bar

On web, a pool page has an address worth sending to somebody:

```
/                       the list, on the default chain
/ethereum               the list, on that chain
/ethereum/0xC09e82…     that pool
```

Flet gives the browser's URL as `page.route`, pushes history entries with
`page.go`, and calls `on_route_change` when either the app or the *user*
navigates — the Back button included. There is no way to tell those two apart,
and no need to: `apply_route` compares the route with what is on screen and
moves only if they differ, so the same handler serves a click, a deep link and
a Back press without looping.

Chain names are the API's own (`xdai`, `x-layer`), because they are what every
other part of this app keys by. A deep link asks the API for that *one* pool
rather than paging until it turns up — it may be below the TVL floor or on page
nine — and a rotted link lands on the list with a message rather than nowhere.

Two consequences worth knowing. The published build routes on the URL **path**,
which a static server knows nothing about, so `tools/serve.py` falls back to
`index.html` for any path that is not a file; deploying elsewhere needs the same
one-line rule. And the in-app back arrow *pushes* the list route rather than
popping history, because Flet exposes no pop — so after using it, the browser's
Back returns to the pool you just left.

None of this touches the desktop build, which has no address bar; `page.route`
is simply never anything but `/`.

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
- **Wheel over the plot** zooms in time, anchored on the candle under the
  pointer so it stays under the pointer.
- **Price follows the visible candles.** Zooming into ten candles rescales the
  price axis to those ten — without it they keep the whole series' range and
  stay squashed into a few pixels, which defeats the point of zooming in.
  pyqtgraph calls the same thing `enableAutoRange`, and the Qt version this is
  modelled on turns it on too.
- **Wheel over the price gutter** scales price on its own, leaving time alone —
  the way a trading chart does it. A Flet `ScrollEvent` carries no modifier
  keys, so the cursor's position is the only thing available to dispatch on,
  and the axis gutter is the conventional target anyway.
- **Dragging vertically** takes the price axis over manually. Any manual price
  gesture switches auto-fit off, so the chart stops fighting you; double-tap
  (or a new series) hands it back.
- **Hover** draws the crosshair: dashed lines through the cursor, the price
  boxed against the left axis, the timestamp boxed on the date axis, and the
  hovered candle's OHLC in the top-left corner rather than trailing the pointer.
  Hover is throttled to 25/s; every event is a round trip into Python, and the
  Qt version throttles for the same reason.
- **Double-tap** refits the whole series.
- **Candle size**, not window: 15m / 30m / 1h / 4h / 6h / 12h / 1d / 7d / 14d, the
  way Curve's own chart does it. Every pair maps to a verified
  `(agg_number, agg_units)` on `lp_ohlc`; `agg_units` accepts only `minute`,
  `hour`, `day`.

**Pool parameters come off the pool, in one call.** The fold under the yields
reads `A`, `gamma`, `fee`, `mid_fee`, `out_fee`, `fee_gamma`,
`offpeg_fee_multiplier`, `price_oracle` and `price_scale` from the contract —
the API supplies the pool and gauge addresses and nothing else there. Which of
them a pool implements is the pool's answer to what family it belongs to, not
the registry name's, so all of them are asked and whatever answers is shown.

That is eleven questions (two have a second, indexed spelling on tricrypto),
and they go in **one** `eth_call` through
[Multicall3](https://github.com/mds1/multicall) — same address on every chain
that has it, `aggregate3` so that a call which is *expected* to fail does not
take the batch down with it. Measured against a public endpoint:

```
multicall       0.05s   1 request    8 values
one at a time   0.58s  12 requests   8 values
```

A chain without Multicall3 is a normal case rather than an error, and nothing
can distinguish it from a batch that answered nothing, so an empty answer falls
back to asking one at a time. The scales differ per parameter — fees are
fractions of 1e10, `gamma`/`fee_gamma`/prices are 1e18 fixed point, the off-peg
multiplier is 1e10 read as a multiplier — and `curve/parameters.py` carries the
mainnet readings the table was built from.

**A pair is charted the way it is written.** "WBTC/USDC" is WBTC priced in
USDC, so it should read ~64,000 and not ~0.0000154. The `/ohlc` endpoint's
parameters are the other way round from how they read — it prices
`reference_token` *in* `main_token` — so the quote coin is what goes in
`main_token`. Taking those names at face value inverts every pair chart, which
is easy to miss unless one side is a stablecoin. The measurement is in
[`docs/curve-api.md`](docs/curve-api.md).

**How many candles is a function of the plot width, not a fixed number.**
`CandlestickChart` draws bodies at a fixed width — measured at ~3 logical
pixels, and unchanged whether it is handed 20 spots or 365 — so the only lever
on how the chart reads is how many candles share the width. A fixed 200 looked
cramped at one candle size and sparse at another. The count is now
`plot_width / TARGET_PITCH_PX` (5.5px), which puts a 3px candle in a 2.5px gap:
gaps never wider than the candles, candles never hairlines. Two consequences
fall out of it — a candle is the same size at every candle size, and widening
the chart shows *more* candles rather than the same ones stretched, which is
why a material resize refetches.

The exception is history: 7d and 14d on a young pool return fewer candles than
the chart has room for (81 weeks is all that exists), so those spread out. That
is the data running out, not the pitch.

No animation on any of it. A 250ms ease flatters a data swap and is actively
wrong under direct manipulation: every drag frame sets a new window, so the
chart spends its time easing towards where the cursor *was*. It felt like
dragging through treacle until the animation came out.

Only the visible window (plus a small margin) is sent to the chart, so a drag
at 1Y serialises ~20 spots rather than 365.

**One bad wick will set the whole scale.** Strategic USD Reserves has a daily
candle whose low is `0.024` against a body of `1.0158` — an API glitch, not a
two-cent trade in a USDC/USDT pool — and fitting the axis to the absolute
min/max flattened 200 days of history into a line at the top of the chart. The
price axis is therefore fitted to the candle *bodies* plus any wick within
`WICK_HEADROOM` (3×) of that range. The rule is relative to how much the series
actually moves, so a genuine 1.2% dip on the same pool still sets the scale
while the 97% one does not; a body is never excluded; and the outlier is not
deleted, just drawn clipped, so panning down still reaches it.

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

## Logos

Chain marks, token images and the Curve logo come from
[curve-assets](https://github.com/curvefi/curve-assets), carried as a submodule
at `vendor/curve-assets`. Upstream is 67 MB across 38 networks, so
`tools/build_assets.py` compiles in only what this app can draw — every chain
logo (388 KB for all 40), the wordless Curve mark, and the token images for
**every chain upstream has**, read from the directory listing rather than a
list kept in the tool. That is ~29 MB in `src/assets/curve/`, generated and
gitignored.

The listing matters: a list kept in the tool goes stale silently, because a
token with no image draws its initials and a chain missing *entirely* looks
exactly the same. That is how Gnosis ended up with nothing but lettered
circles — it was never in the list, and nothing said so. Note upstream calls
that chain `xdai`, as Curve's API does.

`build_icons.py` also writes `icons/loading-animation.png`, which is what the
page shows while the Python runtime starts — Flet puts its own logo there, and
it is overridden the same way the favicon is: same file name, copied over
theirs at publish time.

A token with no image upstream still draws initials, but quietly: the hue
derived from its symbol tints the disc and its border, and the letters take
the theme's own colour. White letters on a saturated disc read as a brand
rather than as a missing logo, which was loudest in the swap pickers where one
coin would shout and the other would not.

The app's own icon is the same mark, rendered out of that SVG by
`tools/build_icons.py` (librsvg + Pillow) into `src/assets/favicon.png`,
`icon.png` and `icons/*.png`. Those **are** committed, unlike everything else
derived from the submodule: a site needs a favicon whether or not whoever
cloned it ran `git submodule update --init`, and `flet build` cannot read an
SVG. The names are Flet's own — `flet publish` copies `src/assets` over its web
root, so a file only replaces the stock Flet icon by landing at the same path.
The maskable and Apple variants carry a white backdrop and sit inside the
middle 78%, because those get cropped to a circle or a squircle and iOS
composites transparency onto black.

**Seeing the favicon.** `flet publish` from the repo root, then
`python tools/serve.py 8033`.

It did not show up at first, and the reason is worth keeping. The file was
built, copied and served correctly — `/favicon.png` returned it on demand —
but the tab stayed blank because **the link tag was not in `<head>`**. The
comment at the top of `index.html` explains which markers `patch_index.py`
rewrites, and it spelled out an HTML comment terminator while warning against
writing one. The browser ended the comment there, found bare prose inside
`<head>`, and did what the parser is specified to do: implicitly closed the
head and opened the body. The base tag, the title, the manifest link and both
icon links landed in `<body>`, where a favicon link is ignored — so Chrome fell
back to `/favicon.ico`, got a 404, and drew nothing.

Nothing looks wrong from the outside: the file reads correctly, the icon is
served correctly, and `document.querySelector('link[rel=icon]')` finds it.
`document.head.children` is what gives it away, and
`tests/test_index_html.py` now parses the file the way a browser does and fails
on any bare text inside the head.

The URL carries a `?v=1` as well, since browsers keep favicons in a store keyed
by URL that no cache header reaches; bump it when the mark changes. There is a
real `favicon.ico` too, for the path every browser probes before reading the
link.

**Seeing it on the desktop.** `flet run` gets it too, on X11: the Flutter host
sets no `_NET_WM_ICON` at all and Flet's own `window.icon` is Windows-only, so
`ui/window_icon.py` sets the property itself through libX11 at startup, using
pixels `build_icons.py` pre-decoded into `assets/window_icon.argb` (no image
library in the app, which is what keeps this off the browser build). It finds
the host window by `WM_CLASS` and narrows to the one in its own **process
group** — `flet run` spawns the app and the host as siblings, and a second Flet
app started from another shell must not be stamped, since every Flet window
shares that class. Check it with
`xprop -id $(wmctrl -lx | awk '/flet.Flet/{print $1; exit}') _NET_WM_ICON`, which
prints the six sizes it now carries.
Everywhere else — Wayland without XWayland, macOS, a headless run — it does
nothing at all, and the supported route is `flet build`, which reads
`assets/icon.png`.

A pool draws its coins as overlapping discs, the way Curve's list does, and for
a **metapool that means the underlying assets**: v2 returns
`[metaToken, basePoolLpToken, …underlying]`, so the World Liberty pool reports
`USD1, crv2pool, USDC, USDT` and shows `USD1 · USDC · USDT`. The LP token in the
middle is plumbing, not an asset. It is dropped by *position* rather than by
address — on newer pools the base LP token is the base pool contract and could
be matched, but on older ones (3Crv, crvFRAX) it is a separate contract and
cannot; a Curve metapool always has exactly two real coins, so index 1 is the
reliable part.

**That distinction turned out to be a live bug, not a cosmetic one.** `coins` is
the decomposed list; the *contract* has `n_coins` of them. `add_liquidity` takes
a `uint256[N]` whose N is part of the function signature, so a metapool deposit
built from the decomposed list was calldata for a function the pool does not
have — and the deposit form showed four fields for a two-coin pool. Everything
that reaches the chain now uses `pool.pool_coins` (`coins[:n_coins]`), and
everything the user reads uses `pool.display_coins`. `balances` from the API
line up with the former.

Two mechanical notes worth keeping:

- **On web, an asset needs a real URL.** Flet treats an `Image.src` that does
  not look like a URL as a path into the Flutter *asset bundle*; `flet publish`
  copies `src/assets/**` to the site root instead, which is not in that
  manifest. A relative path finds nothing and a leading slash is not enough
  either, so the browser build builds an absolute URL from the worker's own
  `location` — there is no `window` in a Web Worker, but there is a `location`.
  Desktop keeps the relative path, which `assets_dir` resolves.
- **Do not wrap a positioned `Stack` child in another `Container`.** The first
  version nested the mark inside a positioning container; the images fetched
  with `200`s and the row reserved the right width, but nothing painted and
  `error_content` never fired either. Setting `left`/`top` directly on the mark
  fixed it.

Every logo degrades to a lettered disc, coloured from the symbol so a token
looks the same everywhere. That is not just insurance against a skipped build
step — plenty of long-tail tokens have no image upstream.

## Themes

Three, cycled by the one button in the header: **light**, **dark**, and
**Chad**. The first two are Material's — one seed colour, and the generator
works out the other forty-five slots. Chad is not generated: it is a hand-set
palette taken from [linux.org.ru](https://www.linux.org.ru), which is the
Tango palette — warm aluminium greys under chocolate, butter and orange
accents.

**Getting the palette right took two attempts, and the failure is instructive.**
The obvious move is to read the stylesheet: pull `combined.css`, find the
`:root` block, copy the hex. That produced a theme that was *plausible and
wrong* — every variable name matched, every value was a different colour. Two
reasons. The site ships several sheets and the default is `tango/`, not the
`waltz/` one linked from its settings page; and within tango the `:root` block
appears **twice**, dark first, light second. A grep finds whichever it finds.

The values below were read off the live page instead —
`getComputedStyle(document.documentElement)` on `/forum/talks/` — which is the
only way to know which sheet and which block actually win:

| variable | | role |
| --- | --- | --- |
| `--main-background` | `#D3D7CF` | the page behind everything |
| `--article-background` | `#EEEEEC` | panels, boxes, dialogs |
| `--text-color` | `#3B4245` | body text |
| `--table-border-color` | `#BABDB6` | the rule between rows |
| `--table-hover-background` | `#AD7FA8` | the row under the pointer |
| `--icon-button-active-color` | `#C17D11` | an active control |
| `--tagpage-group-label-background` | `#E9B96E` | a label that wants noticing |
| `--main-menu-color` | `#8F5902` | the navigation |
| `--tag-color` | `#CE5C00` | tags |
| `--link-color` | `#204A87` | an ordinary link |

The mapping into Material's slots is by *role*, not by name, and `ui/theme.py`
lists each one against the variable it came from. The **row highlight is the
tell**: `#AD7FA8`, a flat plum, is the single most recognisable thing about
that site and the one colour no seed-generated palette arrives at. The first
version had it as a pale amber, which is what the wrong stylesheet says.

Material's ink overlay will not produce it on its own — its default hover is a
translucent tint of the surface — so the Chad theme sets Flutter's own
`hover_color`, scoped to the rows by a nested `ft.Theme` on the container that
holds them. Two earlier attempts at that hover are in `git log`, and both are
worth knowing about:

- **`Event.data` for `on_hover` is a bool in Flet 0.86.** Older Flet sent
  `"true"`/`"false"`. Comparing against the string is a handler that fires and
  does nothing, with no error anywhere.
- **A keyed control is frozen once a rebuild has re-diffed it.** Rows carry
  `key="pool-row-N"` so the integration tests can find them; when Flet matches
  an old row to a new one by key it marks the result `_frozen`, and *any*
  assignment to it then raises `Frozen controls cannot be updated`. So an
  `on_hover` that painted `bgcolor` worked until the first theme change or
  window resize and threw on the next hover. Anything a handler needs to
  change must live on a control that is **not** keyed and outlives the
  rebuild — which is why the theme carries the hover colour and why
  `rebuild()` no longer re-makes the rows at all.

**The shadows are the other half of it.** Material's elevation draws a blurred
gradient; Chad draws a hard offset instead — `blur_radius=0`, one constant
opacity, 3px down and right, 2px for things inside a panel, and 3px *straight
down* for the top bar, which reaches both window edges and so has no side to
cast from. That is what a
shadow under a bordered box looked like before shadows became soft. (The site
itself has no shadows at all — `box-shadow` is `none` everywhere on that page.
These are an addition, in the spirit of the rest.) Because the same shadow
under a Material surface would read as a mistake, every caller asks
`theme.panel_shadow(page)`, which returns `None` unless Chad is on.

Two consequences worth knowing:

- **Switching theme rebuilds the view, not just the colours.** Shadows and the
  row hover are set when a control is built, so `_rebuild_view()` re-makes
  whichever view is on screen. Colour alone would repaint fine.
- **Which theme is on is read off the page**, by `theme.is_chad(page)`, rather
  than tracked in a variable — a control built at any moment then asks the same
  question and gets the current answer.

The button shows the theme you are **in**: a sun for light, a moon for dark,
and the Chad himself for Chad — `chad.png` from
[curve-frontend](https://github.com/curvefi/curve-frontend/blob/main/packages/ui/src/images/chad.png),
which is why it is committed to `src/assets` rather than compiled out of the
curve-assets submodule like everything else. Showing the theme a click would
*get* you is what this did first, and it is unreadable: a moon on a plainly
light screen says the opposite of what is true. The destination goes in the
tooltip, where there is room to say it in words. Drawing an image is also why
the button is a `Container` and not an `IconButton`, which takes only an icon.

The choice is remembered in `SharedPreferences` under `flet-curve.theme` — the
browser's storage on web, a file on the desktop — and put back on load. Both
halves of that API are **coroutines**, and calling one without `await` fails
silently: the write never happens and the read returns a coroutine object that
no `isinstance` will match. The restore therefore runs as a task, so the first
paint is in the default theme and the saved one arrives just after; failure at
either end is swallowed, because a private window with no storage should still
open the app.

## Responsive layout

Every responsive decision comes from one pure function, `layout_for(width)` in
`ui/responsive.py`, and each view is *told* the layout rather than measuring
anything itself. Three breakpoints:

| width | pool list | pool page |
|---|---|---|
| ≥ 1000 | five columns | chart beside the actions |
| 900–1000 | drops **Base APY** | chart beside the actions |
| 760–900 | drops **Base APY** | chart above the actions |
| < 760 | **cards**, sort dropdown | chart above the actions |

Base APY goes first because it is the least decisive number on the row. Below
760px no amount of width-juggling saves five columns, so a row becomes a card:
identity on one line, the figures underneath, each with its own label since
there are no column headers left to read them against — and the headers are
replaced by a sort dropdown, because you cannot click a column that is not
there.

Two things this exposed, both real bugs rather than cosmetics:

- **`page.on_resize` fires on changes, not at startup**, so a window that
  *opened* narrow never learned it was narrow. The layout is now applied on
  first load as well.
- **A flex child inside a scrolling Column is a Flutter layout error**, not a
  visual glitch. The stacked pool page scrolls the page, so the action panel
  there takes a fixed height instead of `expand` — the two arrangements need
  opposite scrolling, which is why `_arrange` rebuilds rather than reflows.

### Testing it without a window

The breakpoints are a pure function of a number, so `tests/test_responsive.py`
checks them exhaustively — at real device widths, at the exact boundaries, and
that columns only ever *shrink* on the way down — with no window at all.
`tests/test_views.py` then checks that each view actually reconfigures.

That matters because **Flet's integration tests cannot resize the page in
device mode** (`flet_app.page` is not bound; `resize_page` raises "page is not
initialized", and host mode wants a Flutter test host this project does not
provision). The UI suite therefore runs at whatever the test surface is — which
turns out to be under 760px, so **those seven tests exercise the phone
layout**. They caught the stacked-layout crash above. The wide layout is
covered by the unit tests plus a browser pass.

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

## Checks

```bash
uv pip install --python .venv/bin/python mypy ruff hypothesis   # once
.venv/bin/python tools/check.py                       # ruff, mypy, pytest
.venv/bin/python tools/check.py --fix                 # let ruff fix first
```

Both tools are configured in `pyproject.toml`, so an editor and this script
agree by construction. Ruff earned its place immediately: it found an
`except WalletError` in a module that never imported the name, so the handler
written to swallow a failed disconnect would have raised `NameError` instead.
That one is now pinned by a test.

`ruff format` is deliberately **not** part of this. The formatting here is
hand-set — aligned comments, tables in docstrings, wrapped prose — and letting
a formatter reflow it would cost more than it returns.

Mypy checks `src` and `tools` with nothing excused. The tests are checked too,
but with the error codes a test double trips by existing switched off
(`arg-type`, `attr-defined`, `method-assign` and friends): a fake API passed
where `CurveApi` is declared is the point of the file, not a mistake in it.

One thing worth knowing about Flet's own hints, since they are good but not
complete: `ft.ControlEvent` is `Event[BaseControl]` to a type checker, and
`Event` is invariant, so a handler annotated with Flet's own alias cannot be
passed to `TextField(on_change=…)`, which wants `Event[TextField]`. Naming the
concrete control does not work either, because several handlers here are shared
between a TextField, a Dropdown and a RadioGroup. `ui.AnyEvent` is the accurate
type — `Event[Any]`, which is what the alias resolves to at runtime anyway.

## Testing

Four layers, and the first three need nothing at all:

```bash
.venv/bin/python -m pytest tests/ -q                      # 634 tests, ~9s
HYPOTHESIS_PROFILE=deep .venv/bin/python -m pytest tests/test_stateful.py
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

**Looking at it.** Three environment variables open the app somewhere other
than the front page, which is the difference between one command and a
sequence of hovers and clicks — and on the desktop build there is no address
bar to shortcut with:

```bash
CURVE_ROUTE=/ethereum/portfolio CURVE_THEME=chad .venv/bin/flet run src/main.py
CURVE_WINDOW=390x844 .venv/bin/flet run src/main.py       # a phone
```

`CURVE_ROUTE` takes any route the address bar accepts; `CURVE_THEME` takes
`light`, `dark` or `chad` and skips the remembered one; `CURVE_WINDOW` takes
`WIDTHxHEIGHT`. All three are ignored when unset, so a normal launch is
unchanged.

`CURVE_WINDOW` is not the same test as resizing a window down to a phone's
width, and the difference is the point: opening narrow paints once, resizing
paints twice, and a control that is *told* about the new layout but never
*asked to redraw* looks correct in the first case and wrong in the second.
That is exactly the shape of the bug it was added to chase down.

That matters more than it sounds, because **the desktop window can be captured
from the X server** and the browser cannot be trusted to screenshot itself:

```bash
wmctrl -lx | grep flet.Flet             # find the window
import -window 0x07a00003 shot.png      # ImageMagick, whole window
```

Chrome's DevTools screenshots of this app intermittently come back missing
every token logo — text renders, images do not — which sent a long hunt after
a bug that was never in the app. An `import -window` capture of the real
window has never lied. It is also how the logo sampling was chosen: the same
mark rendered eight ways at 27px, captured, and magnified.

**3. Stateful tests — `test_stateful.py`.** The layers above check one
transition each. This one checks that no *ordering* of transitions leaves the app
broken, which is a different question and the one that had been going unasked:
the bug that shipped was "switch theme a few times, then hover a row", and every
step of it passed its own test.

A Hypothesis `RuleBasedStateMachine` drives the real `CurveApp` — theme cycling,
sorting, searching, resizing, chain switches, opening a pool, Back, deep links —
and fires handlers it picks out of the live control tree by index rather than by
name, so it reaches handlers written after it. Invariants assert what must always
hold: the theme's decorations match the theme, the route matches what is on
screen, and nothing the app will later assign to has been frozen.

**What makes it more than a smoke test is that it runs Flet's real diff.**
`tests/fake_session.py` calls `ObjectPatch.from_diff` and then serialises what
the diff reports as added — the two halves of a real update — so a keyed control
that a rebuild re-made ends up `_frozen` exactly as it does behind a browser.
Put the shipped hover bug back and the machine finds it in about four seconds,
shrunk to a handful of steps. It also found one that had not been noticed:
switching theme while a pool page was open left the *list* wearing the old
theme, so pressing Back landed on a table with no border, no shadow and the
wrong hover.

Two profiles: the default keeps it to a few seconds so it stays part of
`check.py`; `HYPOTHESIS_PROFILE=deep` runs 500 examples of 80 steps (~7 minutes)
for when something smells.

**4. UI tests — `flet.testing`.** Seven tests that start the real app and drive it:
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

- **A `key` makes a control read-only after its first rebuild.** When Flet
  re-diffs a list and matches an old item to a new one by `key`, the survivor
  is marked `_frozen`, and every property assignment on it then raises
  `Frozen controls cannot be updated` — from inside an event handler, where it
  surfaces as an unhandled error and not as anything the caller can catch. Keys
  are for finding controls (integration tests, in this app); state that
  handlers mutate belongs on an unkeyed control that outlives the rebuild.
- **`SharedPreferences.get`/`set` are coroutines**, and `page.shared_preferences`
  is deprecated in 0.86 (removed in 0.90) in favour of constructing
  `ft.SharedPreferences()`, which registers itself with the page. Calling either
  method without `await` fails silently — see the themes section.
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

- **No router.** Swaps are `exchange` — or `exchange_underlying` on a metapool,
  which is still one pool doing both legs itself. Cross-pool routing is a
  different problem and a much larger one.
- **No balanced-deposit helper.** Deposits go to `add_liquidity` with explicit
  per-coin amounts. (Metapool underlying deposits *are* built, through the
  zaps — see below.)
- **Withdrawal floors on the balanced path** are derived from the reserves the API
  reports rather than from `calc_token_amount(…, is_deposit=False)`. Sending zero
  floors would be simpler and is what many UIs do; it also offers no protection
  against a sandwich.
- **No claim-rewards button.** Staking and unstaking are there; `mint`/
  `claim_rewards` are not.
