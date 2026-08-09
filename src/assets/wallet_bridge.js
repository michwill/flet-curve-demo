/*
 * wallet_bridge.js -- the main-thread half of the browser wallet transport.
 *
 * Flet runs your Python in a Web Worker. Wallets live on the main thread
 * (`window.ethereum`, EIP-6963 announcements, every WalletConnect/wagmi
 * modal). This script sits where the wallet is and relays EIP-1193 calls to
 * and from Python over a BroadcastChannel.
 *
 * Loaded by index.html *before* the Flutter bootstrap, so by the time
 * Pyodide is up the bridge is already listening.
 *
 * Wire protocol (see src/wallet/browser.py -- both sides must agree):
 *   in   {v:1, dir:"req", id, method, params}
 *   out  {v:1, dir:"res", id, result}  |  {v:1, dir:"res", id, error:{code,message}}
 *   out  {v:1, dir:"evt", event, data}
 *
 * ---------------------------------------------------------------------
 * CONNECTORS
 * ---------------------------------------------------------------------
 * Everything below the `CONNECTORS` array is connector-agnostic, because
 * every wallet connector in the ecosystem bottoms out at the same EIP-1193
 * object:
 *
 *   - EIP-6963 injected wallets  -> event.detail.provider
 *   - WalletConnect v2           -> EthereumProvider.init(...)   IS one
 *   - Coinbase Wallet SDK        -> sdk.makeWeb3Provider()
 *   - wagmi (any connector)      -> await connector.getProvider()
 *
 * So adding a connector means adding one entry to CONNECTORS with a
 * `list()` and a `resolve()`. Nothing else in this file changes, and
 * *nothing* on the Python side changes -- the wire protocol is the
 * contract.
 */

(() => {
  "use strict";

  const CHANNEL_NAME = "flet-wallet";
  const VERSION = 1;

  // Which wallet was last connected, so closing the tab does not mean
  // starting over. Kept in the page rather than in Python: a Pyodide
  // worker has no localStorage, and this is the only side that has one.
  //
  // The `rdns` is the stable identity -- a wallet's EIP-6963 uuid is
  // generated per page load -- so what is stored is matched against
  // whatever is announced next time, and simply misses if the extension
  // was uninstalled.
  const REMEMBER_KEY = "flet-wallet:last";

  function remember(info) {
    try {
      localStorage.setItem(
        REMEMBER_KEY,
        JSON.stringify({ rdns: info.rdns || "", connector: info.connector || "" })
      );
    } catch (_) {
      /* private mode, or storage disabled: connecting still works */
    }
  }

  function remembered() {
    try {
      return JSON.parse(localStorage.getItem(REMEMBER_KEY) || "null");
    } catch (_) {
      return null;
    }
  }

  function forget() {
    try {
      localStorage.removeItem(REMEMBER_KEY);
    } catch (_) {
      /* nothing to clean up */
    }
  }

  // A BroadcastChannel is shared by every page on the origin, so a second
  // tab running this app hears -- and would answer -- the first tab's
  // requests. Two bridges answering one client is not cosmetic: whichever
  // replies first wins, so the wallet list can come from one tab while
  // `selectWallet` lands on the other, and the account request then goes
  // to a wallet nobody picked. That is exactly what a second tab looked
  // like: a picker missing the wallets this page announced.
  //
  // The fix is that only one bridge per origin is ever active. Web Locks
  // settle it without any coordination: whoever takes the lock serves
  // every client on the origin, and the browser hands it to another tab
  // the moment that one goes away. Requests are addressed by `client` so
  // each app only ever sees its own replies and its own wallet events.
  //
  // Pairing them per tab instead is not possible here: a Pyodide worker
  // cannot tell which page spawned it, and a BroadcastChannel message
  // carries no sender.
  let active = true;
  if (navigator.locks?.request) {
    active = false;
    navigator.locks
      .request("flet-wallet-bridge", () => {
        active = true;
        log("serving this origin");
        // Held until the page goes away, which releases it for the next
        // tab. Nothing resolves this promise on purpose.
        return new Promise(() => {});
      })
      .catch(() => {
        // No lock support, or it was denied: better a bridge that answers
        // than a page that cannot reach a wallet at all.
        active = true;
      });
  }

  // Mutable: Python fills this in via bridge_configure at startup.
  const config = window.FLET_PAY || (window.FLET_PAY = {});
  const channel = new BroadcastChannel(CHANNEL_NAME);

  /** uuid -> {info, resolve} for everything any connector has offered. */
  const catalogue = new Map();
  /** The EIP-1193 provider all non-bridge methods are forwarded to. */
  let selected = null;
  let selectedInfo = null;
  let detachers = [];

  const log = (...args) => console.log("[wallet-bridge]", ...args);

  // ====================================================================
  // Connector 1: injected wallets, discovered via EIP-6963
  // ====================================================================
  //
  // EIP-6963 replaced the old "everyone fights over window.ethereum" mess:
  // each wallet announces itself with an event carrying a stable rdns id.
  // This is exactly the mechanism wagmi's injected() connector uses, so
  // coverage here is identical to wagmi's for extension wallets.

  const announced = new Map();

  window.addEventListener("eip6963:announceProvider", (event) => {
    const { info, provider } = event.detail;
    announced.set(info.uuid, { info, provider });
    log("announced:", info.name, info.rdns);
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));

  function legacyInjected() {
    // Pre-EIP-6963 wallets only set window.ethereum.
    if (!window.ethereum) return null;
    for (const { provider } of announced.values()) {
      if (provider === window.ethereum) return null; // already announced
    }
    const eth = window.ethereum;
    const name = eth.isMetaMask
      ? "MetaMask (legacy)"
      : eth.isRabby
        ? "Rabby (legacy)"
        : eth.isFrame
          ? "Frame (legacy)"
          : "Injected wallet";
    return { info: { uuid: "legacy:window.ethereum", name, rdns: "legacy" }, provider: eth };
  }

  const injectedConnector = {
    id: "injected",
    async list() {
      window.dispatchEvent(new Event("eip6963:requestProvider"));
      // Announcements are synchronous in practice, but a freshly-installed
      // or still-initialising extension can miss the first dispatch. One
      // frame of slack avoids a spurious "no wallet found".
      await new Promise((resolve) => setTimeout(resolve, 350));

      const entries = [...announced.values()];
      const legacy = legacyInjected();
      if (legacy) entries.push(legacy);

      return entries.map(({ info, provider }) => ({
        info: {
          uuid: info.uuid,
          name: info.name,
          rdns: info.rdns,
          icon: info.icon || null,
          connector: "injected",
        },
        resolve: async () => provider,
      }));
    },
  };

  // ====================================================================
  // Connector 2: WalletConnect v2 -- mobile wallets by QR / deep link
  // ====================================================================
  //
  // `EthereumProvider` from @walletconnect/ethereum-provider is itself an
  // EIP-1193 provider, which is why it drops straight into this design:
  // resolve() returns it and the forwarding code below is unchanged.
  //
  // Requires a projectId from https://dashboard.reown.com -- set it in
  // index.html. Without one this connector simply does not appear, because
  // offering an entry that cannot possibly work is worse than omitting it.
  //
  // The module is imported at *selection* time, not page load, so a visitor
  // who uses MetaMask never pays for the download.

  // Read at call time, not load time, so every knob in window.FLET_PAY
  // behaves the same way and can be changed from the console while
  // debugging. (Capturing these in a const at load was a trap: projectId
  // was late-bound and the URL was not, so overriding the URL silently
  // did nothing.)
  const wcModuleUrl = () =>
    config.walletConnectModuleUrl || "https://esm.sh/@walletconnect/ethereum-provider@2";

  // Chains the Python side knows about (src/wallet/chains.py). Sent as
  // `optionalChains` so a wallet that supports only some of them still
  // pairs instead of rejecting the whole session.
  const wcChains = () => config.walletConnectChains || [1, 10, 137, 8453, 42161];

  let wcProvider = null;

  // WalletConnect's EthereumProvider.request() forwards *everything* to the
  // wallet over the relay -- nothing is answered locally. That breaks the
  // handshake this app does after connecting:
  //
  //   eth_requestAccounts -> relayed to the wallet -> most wallets (Safe
  //   included) never answer it, because the session was already approved
  //   during enable(). The promise then hangs forever, and the app sits on
  //   "Waiting for approval..." with no error. (Upstream issue #2092.)
  //
  // enable() has already populated `accounts` and `chainId` on the provider,
  // so answer those four locally and relay only what genuinely needs the
  // wallet. Signing methods must still go over the relay, untouched.
  const WC_LOCAL_METHODS = new Set([
    "eth_accounts",
    "eth_requestAccounts",
    "eth_chainId",
    "net_version",
  ]);

  function wrapWalletConnect(provider) {
    return {
      on: (...args) => provider.on(...args),
      removeListener: (...args) => provider.removeListener(...args),
      async request({ method, params }) {
        if (WC_LOCAL_METHODS.has(method)) {
          switch (method) {
            case "eth_accounts":
            case "eth_requestAccounts":
              return provider.accounts || [];
            case "eth_chainId":
              return `0x${Number(provider.chainId).toString(16)}`;
            case "net_version":
              return String(provider.chainId);
          }
        }
        return await provider.request({ method, params });
      },
    };
  }

  const walletConnectConnector = {
    id: "walletconnect",
    async list() {
      if (!config.walletConnectProjectId) return [];
      return [
        {
          info: {
            uuid: "walletconnect",
            name: "WalletConnect",
            rdns: "org.walletconnect",
            // Injected wallets announce their own icon under EIP-6963.
            // WalletConnect is a protocol, not a wallet, so nothing
            // announces one -- and neither @walletconnect/ethereum-provider
            // nor wagmi's connector exposes a logo (checked: wagmi's only
            // `icon` reference is `iconUrls` for addEthereumChain). So the
            // app ships the mark itself -- from the *Python* side, see
            // wallet/icons.py, because a file in Flet's assets dir is not
            // in the archive Pyodide unpacks and a relative Image src does
            // not resolve on Flutter web. `walletConnectIcon` still wins.
            icon: config.walletConnectIcon || null,
            connector: "walletconnect",
            // Resolving this one is not free. `resolve()` below fetches the
            // module graph -- some nine hundred requests through esm.sh --
            // constructs the Web3Modal, and then opens a QR code. So it
            // must never be chosen on anybody's behalf: it is a wallet you
            // ask for, not one that happens to be here. `browser.py` reads
            // this before pre-selecting a lone wallet, which is exactly the
            // case a browser with no extension installed lands in.
            deliberate: true,
          },
          resolve: async ({ silent } = {}) => {
            if (wcProvider) return wcProvider;

            const moduleUrl = wcModuleUrl();
            log("loading WalletConnect from", moduleUrl);
            const { EthereumProvider } = await import(
              /* @vite-ignore */ moduleUrl
            );

            wcProvider = await EthereumProvider.init({
              projectId: config.walletConnectProjectId,
              // metadata.url must match the serving origin or wallets
              // will show a domain-mismatch warning.
              metadata: {
                name: document.title || "Flet Pay",
                description: "Send tokens from a Flet app written in Python",
                url: window.location.origin,
                icons: [`${window.location.origin}/icons/apple-touch-icon-192.png`],
              },
              optionalChains: wcChains(),
              optionalMethods: [
                "eth_sendTransaction",
                "personal_sign",
                "eth_signTypedData_v4",
              ],
              optionalEvents: ["accountsChanged", "chainChanged"],
              showQrModal: true,
            });

            // enable() is the EIP-1193-compatible connect: it opens the QR
            // modal and resolves once the phone has approved. Doing it here
            // means the modal appears when the user picks WalletConnect,
            // which is the UX people expect.
            if (!wcProvider.session) {
              // init() restores a session from WalletConnect's own storage
              // if the phone is still paired. Without one there is nothing
              // to restore, and a silent call must not open a QR modal
              // nobody asked for.
              if (silent) {
                throw { code: 4900, message: "No WalletConnect session to restore" };
              }
              await wcProvider.enable();
            }
            log("session established, accounts:", wcProvider.accounts);
            return wrapWalletConnect(wcProvider);
          },
        },
      ];
    },
  };

  // ====================================================================
  // Registry -- add a connector by adding an entry here.
  // ====================================================================
  //
  // To add Coinbase Wallet SDK, Safe, or any wagmi connector, follow the
  // same two-method shape. For wagmi specifically:
  //
  //   import { createConfig, connect } from '@wagmi/core'
  //   ...
  //   list()    -> config.connectors.map(c => ({info: {...}, resolve: ...}))
  //   resolve() -> await connect(config, {connector: c}) then c.getProvider()

  const CONNECTORS = [injectedConnector, walletConnectConnector];

  async function discover() {
    const results = await Promise.allSettled(CONNECTORS.map((c) => c.list()));

    catalogue.clear();
    const wallets = [];
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        // One broken connector must never take down discovery.
        log(`connector "${CONNECTORS[index].id}" failed:`, result.reason);
        return;
      }
      for (const entry of result.value) {
        catalogue.set(entry.info.uuid, entry);
        wallets.push(entry.info);
      }
    });
    return wallets;
  }

  // ====================================================================
  // Event forwarding
  // ====================================================================

  function emit(event, data) {
    // Addressed to whoever selected this wallet, not broadcast: another
    // tab's app must not act on an account change it did not ask for.
    channel.postMessage({ v: VERSION, dir: "evt", event, data, client: owner });
  }

  function attach(provider) {
    for (const detach of detachers) detach();
    detachers = [];
    if (!provider || typeof provider.on !== "function") return;

    for (const name of ["accountsChanged", "chainChanged", "disconnect", "connect"]) {
      const handler = (data) => {
        log("event:", name, data);
        emit(name, data);
      };
      provider.on(name, handler);
      detachers.push(() => {
        try {
          provider.removeListener(name, handler);
        } catch (_) {
          /* not every provider implements removeListener */
        }
      });
    }
  }

  //: The client whose selection is live, so events can be addressed.
  let owner = null;

  async function selectWallet(uuid, client, options) {
    const entry = catalogue.get(uuid);
    if (!entry) throw { code: 4001, message: `Unknown wallet: ${uuid}` };

    // resolve() may open a modal and wait on a phone, so it is async and
    // may reject (user closed the QR). That propagates to Python as a
    // normal EIP-1193 error. `silent` is the restore path: resolve without
    // showing anything, and fail rather than prompt.
    selected = await entry.resolve(options || {});
    selectedInfo = entry.info;
    owner = client ?? owner;
    attach(selected);
    remember(entry.info);
    log("selected:", entry.info.name);
    return {
      uuid,
      name: entry.info.name,
      rdns: entry.info.rdns,
      connector: entry.info.connector || "",
    };
  }

  // ====================================================================
  // Request handling
  // ====================================================================

  async function handle(method, params, client) {
    // Bridge-only methods: answered here, never forwarded to a wallet.
    if (method === "bridge_configure") {
      // Settings arrive from Python (wallet/settings.py) rather than a JS
      // file, so that a plain `flet publish` yields a configured build.
      // Anything already set in window.FLET_PAY by index.html wins, so a
      // deployment can still pin values in the page.
      const incoming = params[0] || {};
      for (const [key, value] of Object.entries(incoming)) {
        if (config[key] === undefined || config[key] === "") config[key] = value;
      }
      log("configured:", Object.keys(incoming).join(", ") || "(nothing)");
      return { ok: true, keys: Object.keys(incoming) };
    }
    if (method === "bridge_hello") {
      return {
        ok: true,
        wallets: await discover(),
        selected: selectedInfo?.uuid ?? null,
        remembered: remembered(),
      };
    }
    if (method === "bridge_selectWallet") {
      return await selectWallet(params[0], client, params[1] || {});
    }
    if (method === "bridge_forget") {
      forget();
      return { ok: true };
    }
    if (method === "bridge_listWallets") {
      return await discover();
    }

    if (!selected) {
      const wallets = await discover();
      if (wallets.length === 0) {
        throw { code: 4900, message: "No wallet available in this browser" };
      }
      await selectWallet(wallets[0].uuid, client);
    }

    // The entire connector integration, once resolved: one EIP-1193 call.
    return await selected.request({ method, params });
  }

  channel.onmessage = async (event) => {
    const message = event.data;
    if (!message || message.v !== VERSION || message.dir !== "req") return;

    // Dormant: another tab's bridge holds the lock and is answering.
    if (!active) return;

    const client = message.client ?? null;
    if (message.method === "bridge_release") return;

    const { id, method, params } = message;
    try {
      const result = await handle(method, params || [], client);
      channel.postMessage({ v: VERSION, dir: "res", id, result: result ?? null, client });
    } catch (error) {
      // Normalise the shapes wallets throw: EIP-1193 ProviderRpcError, a
      // plain {code,message} object, and a bare Error.
      channel.postMessage({
        v: VERSION,
        dir: "res",
        id,
        client,
        error: {
          code: Number.isInteger(error?.code) ? error.code : -32603,
          message: String(error?.message || error || "Wallet request failed"),
          data: error?.data ?? null,
        },
      });
    }
  };

  log(
    "ready on BroadcastChannel:",
    CHANNEL_NAME,
    "| connectors:",
    CONNECTORS.map((c) => c.id).join(", "),
    config.walletConnectProjectId ? "" : "(WalletConnect idle: no projectId)"
  );
})();
