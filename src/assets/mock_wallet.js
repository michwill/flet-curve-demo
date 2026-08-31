// mock_wallet.js -- a fake EIP-6963 wallet, for development only.

(() => {
  "use strict";

  // `?mock=0x…` points the mock at a real address instead of the default
  // one, which is what makes the portfolio and the claim tabs testable at
  // all: the default holds nothing anywhere, so every panel that appears
  // only when there is something to show stays hidden. Read-only -- this
  // mock signs nothing, it just answers `eth_accounts` with the address.
  const ASKED = new URLSearchParams(location.search).get("mock") || "";
  const ACCOUNT = /^0x[0-9a-fA-F]{40}$/.test(ASKED)
    ? ASKED
    : "0x1111111111111111111111111111111111111111";
  // Mutable, because the app now asks the wallet to follow the network
  // picker and a mock that always answers "Ethereum" would make that
  // impossible to see working.
  let CHAIN_ID = "0x1";
  // : Networks this mock claims to know. Anything else gets 4902, which
  // is : how a real wallet says "never heard of it" -- and is the path
  // the app : takes to offer `wallet_addEthereumChain`.
  const KNOWN_CHAINS = new Set(["0x1", "0x64", "0xa4b1", "0xa", "0x89", "0x2105"]);
  const NATIVE_BALANCE = 2000000000000000000n; // 2 ETH
  const TOKEN_BALANCE = 1234560000n; // 1234.56 at 6 decimals
  const TOKEN_DECIMALS = 6n;
  const TOKEN_SYMBOL = "TEST";
  // : The block every mock transaction lands in.
  const MINED_BLOCK = 21_000_000;

  // EIP-6963 requires an announced icon, so the mock announces one too --
  // otherwise the app's icon path is never exercised in development.
  const ICON =
    "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+PHJlY3Qgd2lkdGg9IjY0IiBoZWlnaHQ9IjY0IiByeD0iMTQiIGZpbGw9IiM2NzUwQTQiLz48cGF0aCBkPSJNMTQgNDZWMThoOWw5IDE2IDktMTZoOXYyOGgtOFYzMmwtNyAxMmgtNmwtNy0xMnYxNHoiIGZpbGw9IiNmZmYiLz48L3N2Zz4=";

  const info = {
    uuid: "mock-wallet-0000-0000-0000-000000000000",
    name: "Mock Wallet (dev)",
    rdns: "dev.flet-pay.mock",
    icon: ICON,
  };

  // Whether this mock claims EIP-5792.  Opt-in, so the ordinary two-prompt
  // path is what development sees unless batching is what is being tested.
  const BATCHING =
    new URLSearchParams(location.search).get("batch") === "1" ||
    window.__batching === true;

  const word = (hex) => hex.replace(/^0x/, "").padStart(64, "0");
  const utf8Hex = (text) =>
    [...new TextEncoder().encode(text)].map((b) => b.toString(16).padStart(2, "0")).join("");

  const provider = {
    isMockWallet: true,
    _handlers: {},
    on(event, handler) {
      (this._handlers[event] = this._handlers[event] || []).push(handler);
    },
    removeListener(event, handler) {
      this._handlers[event] = (this._handlers[event] || []).filter((h) => h !== handler);
    },
    // Fire a wallet event at the app, e.g.
    // mockWallet.emit('chainChanged', '0xa')
    emit(event, data) {
      (this._handlers[event] || []).forEach((h) => h(data));
    },

    async request({ method, params = [] }) {
      console.log("[mock-wallet]", method, JSON.stringify(params));
      switch (method) {
        case "eth_requestAccounts":
        case "eth_accounts":
          return [ACCOUNT];
        case "eth_chainId":
          return CHAIN_ID;
        case "net_version":
          return String(parseInt(CHAIN_ID, 16));
        case "eth_getBalance":
          return "0x" + NATIVE_BALANCE.toString(16);
        case "eth_call": {
          const selector = (params[0]?.data || "").slice(2, 10);
          // fee() -- 1_500_000 of 1e10, i.e. 0.015%, which is
          // 3pool's.
          if (selector === "ddca3f43") return "0x" + word("16e360");
          // dynamic_fee(int128,int128) is deliberately *not*
          // answered: only StableSwap-NG has it, and the fallback
          // to fee() is the path worth exercising here.
          if (selector === "dd62ed3e") {
            // allowance(): zero until an approval has been
            // mined, so the app's "wait, then re-read" path has
            // something to observe.
            return "0x" + word((window.__approved ? 2n ** 200n : 0n).toString(16));
          }
          if (selector === "313ce567") return "0x" + word(TOKEN_DECIMALS.toString(16)); // decimals()
          if (selector === "70a08231") return "0x" + word(TOKEN_BALANCE.toString(16)); // balanceOf()
          if (selector === "95d89b41" || selector === "06fdde03") {
            // symbol()/name() as a proper dynamic ABI string
            const body = utf8Hex(TOKEN_SYMBOL);
            return (
              "0x" +
              word("20") +
              word(TOKEN_SYMBOL.length.toString(16)) +
              body.padEnd(64, "0")
            );
          }
          return "0x";
        }
        case "eth_blockNumber":
          // Creeps forward so the "wait for the endpoint to catch
          // up" path is exercised rather than short-circuited.
          this._head = (this._head || MINED_BLOCK - 2) + 1;
          return "0x" + Math.min(this._head, MINED_BLOCK).toString(16);
        case "eth_getTransactionReceipt":
          // Pending for the first couple of polls, then mined.
          this._polls = (this._polls || 0) + 1;
          if (this._polls < 3) return null;
          return {
            transactionHash: params[0],
            blockNumber: "0x" + MINED_BLOCK.toString(16),
            status: "0x1",
          };
        case "eth_sendTransaction":
          this._polls = 0;
          // An approve() to any spender counts, for the same
          // reason.
          if ((params[0]?.data || "").startsWith("0x095ea7b3")) {
            window.__approved = true;
          }
          window.__lastTx = params[0];
          console.log("[mock-wallet] would send:", JSON.stringify(params[0], null, 2));
          return "0x" + "ab".repeat(32);
        // -- EIP-5792 ---------------------------------------------------
        // Off unless asked for, so the two-prompt path stays the default
        // one development exercises: `?mock=1&batch=1`, or set
        // `window.__batching` from the console and reconnect.
        case "wallet_getCapabilities": {
          if (!BATCHING) throw { code: 4200, message: "no capabilities here" };
          const wanted = (params && params[1]) || [CHAIN_ID];
          const answer = {};
          for (const chain of wanted) {
            answer[chain] = { atomic: { status: "supported" } };
          }
          console.log("[mock-wallet] capabilities:", JSON.stringify(answer));
          return answer;
        }
        case "wallet_sendCalls": {
          if (!BATCHING) throw { code: 4200, message: "no batching here" };
          const call = params[0] || {};
          const calls = call.calls || [];
          // Same bookkeeping a single send does, so whatever the batch
          // approves counts as approved afterwards.
          for (const one of calls) {
            if ((one.data || "").startsWith("0x095ea7b3")) window.__approved = true;
          }
          this._polls = 0;
          window.__lastBatch = call;
          console.log(
            "[mock-wallet] would send " + calls.length + " call(s) as one batch:",
            JSON.stringify(call, null, 2),
          );
          return { id: "0x" + "ba".repeat(16) };
        }
        case "wallet_getCallsStatus": {
          // Pending for the first couple of polls, like a receipt, so the
          // waiting path is exercised rather than short-circuited.
          this._polls = (this._polls || 0) + 1;
          const calls = (window.__lastBatch || {}).calls || [];
          if (this._polls < 3) {
            return { version: "2.0.0", id: params[0], chainId: CHAIN_ID,
                     status: 100, atomic: true, receipts: [] };
          }
          return {
            version: "2.0.0",
            id: params[0],
            chainId: CHAIN_ID,
            status: 200,
            atomic: true,
            // Atomic, so the whole batch is one transaction and one receipt,
            // however many calls went into it.
            receipts: [{
              transactionHash: "0x" + "ba".repeat(32),
              blockNumber: "0x" + MINED_BLOCK.toString(16),
              status: "0x1",
              gasUsed: "0x" + (21000 * Math.max(calls.length, 1)).toString(16),
            }],
          };
        }
        case "wallet_switchEthereumChain": {
          const wanted = params[0] && params[0].chainId;
          if (!KNOWN_CHAINS.has(wanted)) {
            throw { code: 4902, message: `Unrecognized chain ID ${wanted}` };
          }
          CHAIN_ID = wanted;
          provider.emit("chainChanged", wanted);
          return null;
        }
        case "wallet_addEthereumChain": {
          const added = params[0] && params[0].chainId;
          console.log("[mock-wallet] would add network:", JSON.stringify(params[0]));
          KNOWN_CHAINS.add(added);
          CHAIN_ID = added;
          provider.emit("chainChanged", added);
          return null;
        }
        default:
          throw { code: 4200, message: `Mock wallet does not implement ${method}` };
      }
    },
  };

  const announce = () =>
    window.dispatchEvent(
      new CustomEvent("eip6963:announceProvider", {
        detail: Object.freeze({ info, provider }),
      })
    );

  window.addEventListener("eip6963:requestProvider", announce);
  announce();

  window.mockWallet = provider;
  console.log("[mock-wallet] announced. Inspect window.__lastTx after a send.");
})();
