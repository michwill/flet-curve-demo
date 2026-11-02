"""The bridge, driven under node as two tabs of the app at once."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRIDGE = ROOT / "src" / "assets" / "wallet_bridge.js"
HARNESS = Path(__file__).resolve().parent / "js" / "bridge_harness.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs node to run the bridge"
)


def run() -> dict:
    """Boot the bridge, announce two wallets, drive it as clients A and B."""
    done = subprocess.run(
        ["node", str(HARNESS), str(BRIDGE)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stderr.strip().splitlines()[-1])


def test_the_bridge_still_boots_and_answers() -> None:
    result = run()

    assert result["selectA"]["result"]["name"] == "alpha"
    assert result["selectB"]["result"]["name"] == "beta"


def test_two_tabs_keep_their_own_wallets() -> None:
    """One `selected` for the origin meant the tab that connected last
    took over the other tab's requests: A picks alpha, B picks beta, and
    A's next call went to beta. It is the same bug behind "change wallet,
    keep the old one if it fails" -- the bridge had already moved.
    """
    result = run()

    assert result["afterA"]["result"] == "answered by alpha"
    assert result["afterB"]["result"] == "answered by beta"
    assert (result["alphaCalls"], result["betaCalls"]) == (1, 1)


WC_HARNESS = Path(__file__).resolve().parent / "js" / "walletconnect_harness.mjs"
SAFE = "0x7a16fF8270133F063aAb6C9977183D9e72835428"


def run_walletconnect(chains: list[int], stored: int, methods=("eth_sendTransaction",)) -> dict:
    """Restore a WalletConnect session approved for `chains`, storage on `stored`."""
    scenario = {
        "accounts": [f"eip155:{chain}:{SAFE}" for chain in chains],
        "methods": list(methods),
        "storedChain": stored,
    }
    done = subprocess.run(
        ["node", str(WC_HARNESS), str(BRIDGE), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stderr.strip().splitlines()[-1])


def test_a_chain_outside_the_session_is_named_as_such() -> None:
    """WalletConnect's own refusal reads as the wallet having stayed put."""
    result = run_walletconnect([4663], stored=4663)

    error = result["switchToEthereum"]["error"]
    assert error["code"] == 4901
    assert error["data"] == {"chains": [4663]}


def test_a_stored_chain_the_session_lacks_is_not_reported() -> None:
    result = run_walletconnect([1, 4663], stored=10)

    assert result["chainBefore"]["result"] == "0x1"


def test_a_wallet_that_can_switch_is_asked() -> None:
    """A proposal naming its own methods drops WalletConnect's defaults."""
    result = run_walletconnect(
        [4663], stored=4663, methods=("eth_sendTransaction", "wallet_switchEthereumChain")
    )

    assert "wallet_switchEthereumChain" in result["proposedMethods"]
    assert result["forwarded"] == [1]
