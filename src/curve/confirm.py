"""Waiting for a transaction, and then for the node to admit it happened.

Sending a transaction returns a hash, not a result. Everything the panels
show afterwards -- the allowance that decides whether Deposit is enabled,
the balances, the LP position -- is read back from the chain, so reading it
the moment the hash arrives shows the state *before* the transaction. That
is what made an approval look like it had done nothing.

So this waits for the receipt. Two things beyond "poll until it appears":

  * a receipt with `status: 0x0` is a mined *failure*. Refreshing after one
    is not wrong, but treating it as success is, so it is raised.

  * the receipt names the block the transaction landed in, and a subsequent
    read is not guaranteed to see that block. An RPC endpoint behind a load
    balancer answers from whichever node takes the request, and those nodes
    are not in lockstep -- one can be a block or two behind, which reads as
    the transaction having been rolled back. So after the receipt, this
    waits until the endpoint's own head has reached that block.

The waiting is bounded. A transaction can sit in the mempool for a long
time, and an app that blocks forever on one is worse than an app that says
it stopped watching.
"""

from __future__ import annotations

import asyncio
from typing import Any

from wallet.base import RpcError, WalletError, WalletProvider

#: How often to ask. Ethereum blocks are ~12s; other chains are faster, and
#: an unanswered poll is cheap.
POLL_INTERVAL = 2.0

#: How long to wait for the receipt before giving up on watching. The
#: transaction is not cancelled -- only the watching stops.
RECEIPT_TIMEOUT = 180.0

#: How long to wait for the endpoint to catch up to the receipt's block.
#: Separate from the receipt timeout because it is a different failure:
#: the transaction is mined, the node is merely behind.
CATCH_UP_TIMEOUT = 30.0


class TransactionFailed(WalletError):
    """Mined, and reverted."""


class StillPending(WalletError):
    """Not mined within the time this app was willing to watch."""


async def wait_for_receipt(
    provider: WalletProvider,
    tx_hash: str,
    *,
    timeout: float = RECEIPT_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll until the transaction is mined. Raise if it failed or timed out."""
    waited = 0.0
    while True:
        try:
            receipt = await provider.transaction_receipt(tx_hash)
        except RpcError:
            # A node that does not know the hash yet is not an error worth
            # surfacing; it is the normal state right after broadcasting.
            receipt = None
        if receipt:
            if _status_of(receipt) == 0:
                raise TransactionFailed(
                    "The transaction was mined but reverted. Nothing changed on chain."
                )
            return receipt
        if waited >= timeout:
            raise StillPending(
                f"{tx_hash[:14]}… has not been mined yet. It may still land; "
                "this app has stopped watching."
            )
        await asyncio.sleep(interval)
        waited += interval


async def wait_for_block(
    provider: WalletProvider,
    block: int,
    *,
    timeout: float = CATCH_UP_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> bool:
    """Wait until the endpoint's head has reached `block`. Did it?

    False means it did not within the timeout, which is a reason to show
    slightly stale numbers rather than to fail: the transaction is mined
    either way.
    """
    if block <= 0:
        return True
    waited = 0.0
    while True:
        try:
            if await provider.block_number() >= block:
                return True
        except (RpcError, ValueError, TypeError):
            # An endpoint that will not answer `eth_blockNumber` cannot be
            # waited for; carry on rather than stall the panel.
            return False
        if waited >= timeout:
            return False
        await asyncio.sleep(interval)
        waited += interval


async def wait_for_confirmation(
    provider: WalletProvider,
    tx_hash: str,
    *,
    timeout: float = RECEIPT_TIMEOUT,
    interval: float = POLL_INTERVAL,
) -> int:
    """Mined, successful, and visible to this endpoint. Returns the block.

    The whole point of the return value is that the caller can tell how
    fresh a subsequent read is: everything after this has been read at or
    after the block the transaction landed in.
    """
    receipt = await wait_for_receipt(
        provider, tx_hash, timeout=timeout, interval=interval
    )
    block = _block_of(receipt)
    await wait_for_block(provider, block, interval=interval)
    return block


def _status_of(receipt: dict[str, Any]) -> int:
    """1 for success, 0 for a mined revert.

    Pre-Byzantium receipts have no status field at all; nothing this app
    talks to is that old, and assuming success there matches what every
    other client does.
    """
    raw = receipt.get("status")
    if raw is None:
        return 1
    try:
        return int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return 1


def _block_of(receipt: dict[str, Any]) -> int:
    raw = receipt.get("blockNumber")
    try:
        return int(raw, 16) if isinstance(raw, str) else int(raw or 0)
    except (TypeError, ValueError):
        return 0
