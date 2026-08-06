"""Waiting for a transaction, and for the node to admit it happened.

This exists because sending returns a hash, not a result: the panel used to
re-read the chain the moment the hash arrived, which is before the
transaction is mined, so an approval landed and left the Deposit button
disabled. Two failures are covered here that "poll for a receipt" alone
does not:

  * a mined *revert* has a receipt too, and treating it as success would
    report a deposit that never happened;
  * the receipt names a block, and the next read is not guaranteed to see
    it -- an endpoint behind a load balancer answers from whichever node
    took the request, and those can be a block or two apart.

Nothing here sleeps: the interval is passed as zero so the loops run at
full speed.
"""

from __future__ import annotations

from typing import Any

import pytest

from curve.confirm import (
    StillPending,
    TransactionFailed,
    wait_for_block,
    wait_for_confirmation,
    wait_for_receipt,
)
from wallet.base import RpcError, WalletProvider

TX = "0x" + "ab" * 32


class FakeNode(WalletProvider):
    """A node that answers a scripted sequence of receipts and heads."""

    def __init__(
        self,
        receipts: list[dict[str, Any] | None] | None = None,
        heads: list[int] | None = None,
    ) -> None:
        self.receipts = list(receipts or [])
        self.heads = list(heads or [])
        self.receipt_calls = 0
        self.head_calls = 0

    async def request(self, method: str, params: list[Any] | None = None) -> Any:
        if method == "eth_getTransactionReceipt":
            self.receipt_calls += 1
            return self.receipts.pop(0) if self.receipts else None
        if method == "eth_blockNumber":
            self.head_calls += 1
            value = self.heads.pop(0) if self.heads else 0
            return hex(value)
        raise AssertionError(f"unexpected {method}")


def receipt(block: int = 100, status: str | int | None = "0x1") -> dict[str, Any]:
    out: dict[str, Any] = {"blockNumber": hex(block)}
    if status is not None:
        out["status"] = status
    return out


# -- the receipt -----------------------------------------------------------


async def test_a_mined_transaction_is_returned() -> None:
    node = FakeNode([receipt(block=42)])
    assert await wait_for_receipt(node, TX, interval=0) == receipt(block=42)


async def test_pending_is_polled_until_it_lands() -> None:
    node = FakeNode([None, None, receipt()])
    await wait_for_receipt(node, TX, interval=0)
    assert node.receipt_calls == 3


async def test_a_mined_revert_is_not_success() -> None:
    """It has a receipt like any other, and reporting it as a deposit that
    happened would be the worst possible lie."""
    node = FakeNode([receipt(status="0x0")])
    with pytest.raises(TransactionFailed, match="reverted"):
        await wait_for_receipt(node, TX, interval=0)


async def test_a_receipt_with_no_status_is_taken_as_success() -> None:
    """Pre-Byzantium receipts have no status. Nothing here talks to a chain
    that old, and every other client assumes the same."""
    node = FakeNode([receipt(status=None)])
    assert await wait_for_receipt(node, TX, interval=0)


async def test_waiting_gives_up_eventually() -> None:
    """A transaction can sit in the mempool for hours; the app must not."""
    node = FakeNode([None] * 50)
    with pytest.raises(StillPending):
        await wait_for_receipt(node, TX, timeout=0, interval=0)


async def test_a_node_that_does_not_know_the_hash_yet_is_not_an_error() -> None:
    """Right after broadcasting, some endpoints raise rather than answer."""

    class Grumpy(FakeNode):
        async def request(self, method, params=None):
            if method == "eth_getTransactionReceipt" and self.receipt_calls == 0:
                self.receipt_calls += 1
                raise RpcError(-32000, "transaction not found")
            return await super().request(method, params)

    node = Grumpy([receipt()])
    assert await wait_for_receipt(node, TX, interval=0)


# -- the block floor -------------------------------------------------------


async def test_a_node_already_past_the_block_needs_no_waiting() -> None:
    node = FakeNode(heads=[105])
    assert await wait_for_block(node, 100, interval=0) is True
    assert node.head_calls == 1


async def test_a_node_behind_the_block_is_waited_for() -> None:
    """The load-balancer case: the receipt says 100, this node says 98."""
    node = FakeNode(heads=[98, 99, 100])
    assert await wait_for_block(node, 100, interval=0) is True
    assert node.head_calls == 3


async def test_a_node_that_never_catches_up_does_not_hang_the_panel() -> None:
    node = FakeNode(heads=[1] * 50)
    assert await wait_for_block(node, 100, timeout=0, interval=0) is False


async def test_an_endpoint_that_will_not_say_is_not_waited_for() -> None:
    class Silent(FakeNode):
        async def request(self, method, params=None):
            if method == "eth_blockNumber":
                raise RpcError(-32601, "method not supported")
            return await super().request(method, params)

    assert await wait_for_block(Silent(), 100, interval=0) is False


# -- both together ---------------------------------------------------------


async def test_confirmation_returns_the_block_it_landed_in() -> None:
    node = FakeNode([None, receipt(block=777)], heads=[777])
    assert await wait_for_confirmation(node, TX, interval=0) == 777


async def test_confirmation_waits_for_the_endpoint_to_see_that_block() -> None:
    """Otherwise the panel re-reads from a node one block behind and shows
    the state as it was before the transaction."""
    node = FakeNode([receipt(block=500)], heads=[498, 499, 500])
    await wait_for_confirmation(node, TX, interval=0)
    assert node.head_calls == 3


async def test_a_failed_transaction_never_gets_as_far_as_the_block() -> None:
    node = FakeNode([receipt(status=0)], heads=[1])
    with pytest.raises(TransactionFailed):
        await wait_for_confirmation(node, TX, interval=0)
    assert node.head_calls == 0
