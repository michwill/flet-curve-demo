"""Desktop transport: JSON-RPC over localhost to Frame / qeth.

This is the easy half, and the reason it is easy is worth stating: Frame
(and qeth, which is Frame-compatible) run an HTTP JSON-RPC server on
127.0.0.1:1248. That endpoint *is* an EIP-1193 provider -- wallet methods
are answered locally by the wallet UI (which is what pops the approval
dialog), and everything else is proxied to the current chain's node. So a
desktop Python app needs no wallet SDK, no keys, and no RPC URL: one HTTP
POST per request and the user approves in the wallet they already trust.

Deliberately stdlib-only (`urllib`). Adding `httpx`/`aiohttp` here would
also add it to the Pyodide dependency set for the web build, for no gain.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from .base import RpcError, WalletProvider, WalletUnavailable

#: Frame's port, which qeth reuses so the same dapps work unchanged.
DEFAULT_ENDPOINT = os.environ.get("FLET_PAY_RPC", "http://127.0.0.1:1248")

#: How the wallet identifies us in its approval dialog. Frame and qeth both
#: display the request Origin so the user can see who is asking.
ORIGIN = "http://flet-pay-example.localhost"

#: A read should fail fast; a signature waits on a human.
READ_TIMEOUT = 30.0
SIGNING_TIMEOUT = 600.0
PROBE_TIMEOUT = 1.5

#: Methods that park until the user clicks approve/reject in the wallet.
_INTERACTIVE = frozenset(
    {
        "eth_requestAccounts",
        "eth_sendTransaction",
        "eth_signTransaction",
        "personal_sign",
        "eth_signTypedData",
        "eth_signTypedData_v3",
        "eth_signTypedData_v4",
        "wallet_addEthereumChain",
    }
)


class DesktopWalletProvider(WalletProvider):
    """EIP-1193 over localhost HTTP JSON-RPC."""

    kind = "desktop"

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, name: str = "") -> None:
        self.endpoint = endpoint
        self.name = name or f"Local wallet ({endpoint})"
        self._id = 0

    # -- transport ---------------------------------------------------------

    def _post(self, payload: dict, timeout: float) -> dict:
        """Blocking POST. Runs on a worker thread via `asyncio.to_thread`."""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": ORIGIN,
                "User-Agent": "flet-pay-example/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # A wallet may answer 4xx/5xx with a JSON-RPC error body; prefer
            # that over the bare status code because it is far more useful.
            try:
                return json.loads(exc.read())
            except Exception:
                raise WalletUnavailable(
                    f"Wallet at {self.endpoint} returned HTTP {exc.code}."
                ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WalletUnavailable(
                f"No wallet is listening on {self.endpoint}.\n"
                "Start Frame (frame.sh) or qeth, unlock it, and try again."
            ) from exc

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params if params is not None else [],
        }
        timeout = SIGNING_TIMEOUT if method in _INTERACTIVE else READ_TIMEOUT
        response = await asyncio.to_thread(self._post, payload, timeout)

        if isinstance(response, dict) and "error" in response and response["error"]:
            error = response["error"]
            if isinstance(error, dict):
                raise RpcError(
                    int(error.get("code", -32603)),
                    str(error.get("message", "Unknown wallet error")),
                    error.get("data"),
                )
            raise RpcError(-32603, str(error))
        return response.get("result") if isinstance(response, dict) else None

    # -- discovery ---------------------------------------------------------

    async def probe(self) -> bool:
        """Is a wallet actually listening? Never prompts, never raises."""
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "eth_chainId",
                "params": [],
            }
            await asyncio.to_thread(self._post, payload, PROBE_TIMEOUT)
            return True
        except Exception:
            return False


async def discover(endpoint: str = DEFAULT_ENDPOINT) -> DesktopWalletProvider:
    """Return a reachable desktop provider, or raise `WalletUnavailable`."""
    provider = DesktopWalletProvider(endpoint)
    if not await provider.probe():
        raise WalletUnavailable(
            f"No wallet is listening on {endpoint}.\n\n"
            "This app talks to a local wallet the same way Frame does:\n"
            "  - Frame  -- https://frame.sh\n"
            "  - qeth   -- run it and leave it open\n\n"
            "Both serve JSON-RPC on 127.0.0.1:1248.\n"
            "Set FLET_PAY_RPC to use a different endpoint."
        )
    # No probe can name the wallet: `web3_clientVersion` is one of the
    # methods proxied straight through to the node, so asking it reports
    # the *node* software ("erigon") and never the wallet. Frame and qeth
    # are interchangeable at this endpoint, so say so and stop guessing.
    provider.name = f"Frame / qeth ({endpoint.removeprefix('http://')})"
    return provider
