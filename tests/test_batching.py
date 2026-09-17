"""One prompt for several calls: the rules all four callers share."""

from __future__ import annotations

import pytest

from curve.vecrv import VeCrvContract
from ui.batching import Batching, NotBatchable
from wallet import batch
from wallet.base import WalletProvider

ACCOUNT = "0x" + "a1" * 20
TOKEN = "0x" + "b2" * 20
POOL = "0x" + "c3" * 20


class Wallet(WalletProvider):
    """A wallet that batches, and keeps whatever it was handed."""

    def __init__(self, *, supports: bool = True) -> None:
        self.supports = supports
        self.asked: list[str] = []
        self.batches: list[dict] = []

    async def request(self, method: str, params=None):
        params = params or []
        self.asked.append(method)
        if method == "wallet_getCapabilities":
            return ({"0x1": {"atomic": {"status": "supported"}}}
                    if self.supports else {})
        if method == "wallet_sendCalls":
            self.batches.append(params[0])
            return {"id": "0xba7c4"}
        if method == "wallet_getCallsStatus":
            return {"status": 200, "atomic": True,
                    "receipts": [{"transactionHash": "0x" + "ab" * 32,
                                  "blockNumber": "0x2a"}]}
        if method == "eth_blockNumber":
            return "0x2a"
        raise AssertionError(f"unexpected {method}")


class Panel(Batching):
    """A caller: the mixin is all any of the four has in common."""

    def __init__(self) -> None:
        self.said: list[str] = []

    def waiting(self, batch_id: str) -> None:
        self.said.append(batch_id)


def approval() -> batch.Call:
    return batch.Call(TOKEN, "0x095ea7b3")


def action() -> batch.Call:
    return batch.Call(POOL, "0x3df02124")


# -- asking the wallet ------------------------------------------------------


async def test_the_answer_is_kept_rather_than_asked_again() -> None:
    """A capability belongs to the wallet and the chain, and a press should
    not wait on a read already done."""
    panel, wallet = Panel(), Wallet()

    assert await panel.wallet_batches(wallet, ACCOUNT, 1)
    assert await panel.wallet_batches(wallet, ACCOUNT, 1)

    assert wallet.asked == ["wallet_getCapabilities"]


async def test_and_asked_again_where_the_caller_would_rather_not_keep_it()\
        -> None:
    panel, wallet = Panel(), Wallet()

    await panel.wallet_batches(wallet, ACCOUNT, 1)
    await panel.wallet_batches(wallet, ACCOUNT, 1, fresh=True)

    assert wallet.asked == ["wallet_getCapabilities"] * 2


async def test_a_wallet_that_moved_is_asked_from_scratch() -> None:
    panel, wallet = Panel(), Wallet()

    await panel.wallet_batches(wallet, ACCOUNT, 1)
    panel.forget_batching()
    await panel.wallet_batches(wallet, ACCOUNT, 1)

    assert wallet.asked == ["wallet_getCapabilities"] * 2


async def test_no_account_is_nothing_to_ask_about() -> None:
    panel, wallet = Panel(), Wallet()

    assert not await panel.wallet_batches(wallet, "", 1)
    assert wallet.asked == []


def test_nothing_is_drawn_as_batching_before_the_wallet_has_said_so() -> None:
    """`batches` is for drawing, which cannot wait on a round trip -- so it
    answers for the state before the question, not for the question."""
    assert not Panel().batches


async def test_and_it_answers_with_what_the_wallet_last_said() -> None:
    panel = Panel()
    await panel.wallet_batches(Wallet(), ACCOUNT, 1)
    assert panel.batches

    panel = Panel()
    await panel.wallet_batches(Wallet(supports=False), ACCOUNT, 1)
    assert not panel.batches


# -- what goes in the batch -------------------------------------------------


async def test_the_approval_goes_in_front_of_what_spends_it() -> None:
    """Calls run in the order they are given, and an allowance granted after
    the call that needs it is an allowance that was not there."""
    panel, wallet = Panel(), Wallet()

    await panel.one_prompt(wallet, ACCOUNT, 1,
                           approvals=[approval()], action=[action()],
                           waiting=panel.waiting, interval=0)

    [handed] = wallet.batches
    assert [call["to"] for call in handed["calls"]] == [TOKEN, POOL]


async def test_a_batch_of_approvals_alone_is_refused() -> None:
    """An allowance granted with nothing to spend it is one left standing,
    which is the outcome an exact approval exists to avoid -- and worse than
    the two prompts batching replaced."""
    panel, wallet = Panel(), Wallet()

    with pytest.raises(NotBatchable):
        await panel.one_prompt(wallet, ACCOUNT, 1,
                               approvals=[approval()], action=[],
                               waiting=panel.waiting, interval=0)

    assert wallet.batches == [], "nothing should have been sent"


async def test_atomicity_is_asked_for_nowhere() -> None:
    """A wallet that will do the sequence without promising it lands in one
    transaction is still worth one prompt instead of two."""
    panel, wallet = Panel(), Wallet()

    await panel.one_prompt(wallet, ACCOUNT, 1, action=[action()],
                           waiting=panel.waiting, interval=0)

    assert wallet.batches[0]["atomicRequired"] is False


async def test_the_caller_is_told_the_id_and_the_block() -> None:
    """The id is what a caller says it is waiting for; the block is what it
    re-reads against, and a read from a node still behind it shows the state
    the batch has already changed."""
    panel, wallet = Panel(), Wallet()

    block = await panel.one_prompt(wallet, ACCOUNT, 1, action=[action()],
                                   waiting=panel.waiting, interval=0)

    assert panel.said == ["0xba7c4"]
    assert block == 0x2A


# -- turning an action into calls -------------------------------------------


async def test_an_action_is_collected_rather_than_described_twice() -> None:
    """The call that goes in the batch is the one the button would have
    sent, because it *is* the one the button would have sent."""
    panel, wallet = Panel(), Wallet()
    contract = VeCrvContract(wallet, ACCOUNT)

    [call] = await panel.collected(contract, lambda: contract.approve(5))

    assert call.data.startswith("0x095ea7b3"), "an approve() and nothing else"
    assert wallet.batches == [] and "eth_sendTransaction" not in wallet.asked


async def test_an_action_that_sends_nothing_is_not_a_batch() -> None:
    panel = Panel()
    contract = VeCrvContract(Wallet(), ACCOUNT)

    async def sends_nothing() -> None:
        return None

    with pytest.raises(NotBatchable):
        await panel.collected(contract, sends_nothing)


async def test_nor_is_one_that_sends_twice_without_waiting() -> None:
    """Two calls out of a collection means the action did something between
    them that this cannot promise anything about."""
    panel = Panel()
    contract = VeCrvContract(Wallet(), ACCOUNT)

    async def twice() -> None:
        await contract.approve(5)
        await contract.approve(6)

    with pytest.raises(NotBatchable):
        await panel.collected(contract, twice)
