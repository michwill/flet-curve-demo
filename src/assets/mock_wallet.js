/*
 * mock_wallet.js -- a fake EIP-6963 wallet, for development only.
 *
 * Loaded ONLY when the page URL carries ?mock=1, so it can never appear in
 * a normal visit. It announces itself exactly like a real extension, which
 * means the whole app -- discovery, the wallet picker, balances, ABI
 * encoding, the send flow, the error paths -- can be exercised end to end
 * without a real wallet, a real chain, or real money.
 *
 *   http://localhost:8000/?mock=1
 *
 * It never signs anything: eth_sendTransaction records the transaction on
 * window.__lastTx and returns a fixed hash, so you can assert on exactly
 * what the Python side built.
 */

(() => {
  "use strict";

  const ACCOUNT = "0x1111111111111111111111111111111111111111";
  const CHAIN_ID = "0x1";
  const NATIVE_BALANCE = 2000000000000000000n; // 2 ETH
  const TOKEN_BALANCE = 1234560000n; // 1234.56 at 6 decimals
  const TOKEN_DECIMALS = 6n;
  const TOKEN_SYMBOL = "TEST";
  //: The block every mock transaction lands in.
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
    /** Fire a wallet event at the app, e.g. mockWallet.emit('chainChanged', '0xa') */
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
          // fee() -- 1_500_000 of 1e10, i.e. 0.015%, which is 3pool's.
          // The app reads it to preset the slippage tolerance.
          if (selector === "ddca3f43") return "0x" + word("16e360");
          // dynamic_fee(int128,int128) is deliberately *not* answered:
          // only StableSwap-NG has it, and the fallback to fee() is the
          // path worth exercising here.
          if (selector === "dd62ed3e") {
            // allowance(): zero until an approval has been mined, so the
            // app's "wait, then re-read" path has something to observe.
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
          // Creeps forward so the "wait for the endpoint to catch up"
          // path is exercised rather than short-circuited.
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
          // An approve() to any spender counts, for the same reason.
          if ((params[0]?.data || "").startsWith("0x095ea7b3")) {
            window.__approved = true;
          }
          window.__lastTx = params[0];
          console.log("[mock-wallet] would send:", JSON.stringify(params[0], null, 2));
          return "0x" + "ab".repeat(32);
        case "wallet_switchEthereumChain":
          return null;
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
