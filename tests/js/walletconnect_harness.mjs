// Runs wallet_bridge.js under node against a stand-in for
// @walletconnect/ethereum-provider that restores a stored session the way
// the real one does: its approved accounts, plus whatever chain storage held.
//
// argv[3] is the scenario: {accounts, methods, storedChain}.
// Prints one JSON line: what the bridge proposed and what each call answered.

import { readFileSync } from "node:fs";

const bridgePath = process.argv[2];
const scenario = JSON.parse(process.argv[3]);

globalThis.__wcScenario = scenario;
globalThis.__wcSeen = { initOptions: null, forwarded: [] };

// Switching as @walletconnect/universal-provider 2.26 does it: local within
// the session, forwarded only if the proposal named the method, else refused.
const fakeModule = `
const { EventEmitter } = await import("node:events");
export class EthereumProvider extends EventEmitter {
  static async init(options) {
    globalThis.__wcSeen.initOptions = options;
    const provider = new EthereumProvider();
    const { accounts, methods, storedChain } = globalThis.__wcScenario;
    provider.session = { namespaces: { eip155: { accounts, methods } } };
    provider.accounts = [...new Set(accounts.map((a) => a.split(":")[2]))];
    provider.chainId = storedChain;
    provider.proposed = [...options.optionalMethods];
    return provider;
  }
  async enable() { throw new Error("no QR in a test"); }
  async request({ method, params }) {
    if (method !== "wallet_switchEthereumChain") return null;
    const wanted = parseInt(params[0].chainId, 16);
    const approved = this.session.namespaces.eip155.accounts
      .map((a) => Number(a.split(":")[1]));
    if (approved.includes(wanted)) {
      this.chainId = wanted;
      this.emit("chainChanged", "0x" + wanted.toString(16));
      return null;
    }
    if (this.proposed.includes(method)) {
      globalThis.__wcSeen.forwarded.push(wanted);
      return null;
    }
    throw new Error("Failed to switch to chain 'eip155:" + wanted + "'. The chain is not approved or the wallet does not support 'wallet_switchEthereumChain' method.");
  }
}
`;

const channels = [];
class FakeChannel {
  constructor(name) {
    this.name = name;
    this.onmessage = null;
    channels.push(this);
  }
  postMessage(data) {
    for (const other of channels) {
      if (other !== this && other.onmessage) other.onmessage({ data });
    }
  }
}

globalThis.BroadcastChannel = FakeChannel;
globalThis.localStorage = {
  store: new Map(),
  getItem(k) { return this.store.get(k) ?? null; },
  setItem(k, v) { this.store.set(k, v); },
  removeItem(k) { this.store.delete(k); },
};
globalThis.document = { title: "test" };
if (!globalThis.navigator) globalThis.navigator = {};
globalThis.window = {
  FLET_PAY: {
    walletConnectProjectId: "test",
    walletConnectModuleUrl:
      "data:text/javascript;base64," + Buffer.from(fakeModule).toString("base64"),
  },
  location: { hostname: "localhost", href: "http://localhost/", origin: "http://localhost" },
  addEventListener() {},
  dispatchEvent() { return true; },
};
globalThis.addEventListener = globalThis.window.addEventListener;
globalThis.dispatchEvent = globalThis.window.dispatchEvent;
globalThis.console = { ...console, log: () => {} };

new Function(readFileSync(bridgePath, "utf8"))();

const app = new FakeChannel("flet-wallet");
let next = 1;
const pending = new Map();
app.onmessage = ({ data }) => {
  if (data.dir !== "res") return;
  const waiting = pending.get(data.id);
  if (waiting) {
    pending.delete(data.id);
    waiting(data);
  }
};

function ask(method, params = []) {
  const id = String(next++);
  return new Promise((resolve) => {
    pending.set(id, resolve);
    app.postMessage({ v: 1, dir: "req", id, client: "A", method, params });
  });
}

const out = {};
await ask("bridge_listWallets");
out.select = await ask("bridge_selectWallet", ["walletconnect", { silent: true }]);
out.chainBefore = await ask("eth_chainId");
out.switchToEthereum = await ask("wallet_switchEthereumChain", [{ chainId: "0x1" }]);
out.chainAfter = await ask("eth_chainId");
out.proposedMethods = globalThis.__wcSeen.initOptions?.optionalMethods ?? null;
out.forwarded = globalThis.__wcSeen.forwarded;

console.error(JSON.stringify(out));
