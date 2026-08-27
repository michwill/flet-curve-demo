"""EIP-5792, in both the spellings wallets actually answer in."""

from __future__ import annotations

import pytest

from wallet import batch
from wallet.base import RpcError, WalletProvider

MAINNET = 1
ACCOUNT = "0x" + "a1" * 20
TOKEN = "0x" + "b2" * 20
POOL = "0x" + "c3" * 20


class Answering(WalletProvider):
    """A wallet that answers from a script and records what it was asked."""

    def __init__(self, **answers) -> None:
        self.answers = answers
        self.asked: list[tuple[str, list]] = []

    async def request(self, method: str, params: list | None = None):
        self.asked.append((method, params or []))
        answer = self.answers.get(method)
        if isinstance(answer, Exception):
            raise answer
        return answer


# -- what a wallet says it can do -------------------------------------------


def test_the_final_spelling_of_the_capability():
    assert batch.reads_as_supported({"0x1": {"atomic": {"status": "supported"}}}, 1)


def test_a_wallet_that_would_be_ready_counts_as_supported():
    """"ready" means it would batch after an upgrade it is offering to do."""
    assert batch.reads_as_supported({"0x1": {"atomic": {"status": "ready"}}}, 1)


def test_a_wallet_that_says_unsupported_is_not_pressed():
    assert not batch.reads_as_supported({"0x1": {"atomic": {"status": "unsupported"}}}, 1)


def test_the_draft_spelling_still_counts():
    """`atomicBatch` with a boolean is what wallets shipped while 5792 was a
    draft, and plenty of them are still on it."""
    assert batch.reads_as_supported({"0x1": {"atomicBatch": {"supported": True}}}, 1)


def test_a_chain_written_with_a_leading_zero_is_the_same_chain():
    assert batch.reads_as_supported({"0x01": {"atomic": {"status": "supported"}}}, 1)


def test_a_capability_on_another_chain_is_not_this_one():
    assert not batch.reads_as_supported({"0xa": {"atomic": {"status": "supported"}}}, 1)


@pytest.mark.parametrize("answer", [None, [], "yes", {"0x1": "yes"}, {"0x1": {}}])
def test_nothing_readable_is_not_support(answer):
    assert not batch.reads_as_supported(answer, 1)


async def test_a_wallet_that_has_never_heard_of_capabilities_says_no():
    """Absence arrives as any of several codes, or as a transport error.  None
    of them is worth telling apart from "no"."""
    provider = Answering(wallet_getCapabilities=RpcError(-32601, "unknown method"))

    assert await batch.supported(provider, ACCOUNT, MAINNET) is False


async def test_support_is_asked_about_this_account_and_this_chain():
    provider = Answering(
        wallet_getCapabilities={"0x1": {"atomic": {"status": "supported"}}})

    assert await batch.supported(provider, ACCOUNT, MAINNET) is True
    assert provider.asked == [("wallet_getCapabilities", [ACCOUNT, ["0x1"]])]


async def test_no_account_is_not_worth_asking_about():
    provider = Answering()

    assert await batch.supported(provider, "", MAINNET) is False
    assert not provider.asked


# -- handing the calls over -------------------------------------------------


async def test_the_calls_go_over_in_one_request():
    provider = Answering(wallet_sendCalls={"id": "0xbatch"})

    got = await batch.send(provider, ACCOUNT, MAINNET, [
        batch.Call(TOKEN, "0xapprove"),
        batch.Call(POOL, "0xdeposit"),
    ])

    assert got == "0xbatch"
    (method, params), = provider.asked
    assert method == "wallet_sendCalls"
    assert params[0]["from"] == ACCOUNT
    assert params[0]["chainId"] == "0x1"
    assert params[0]["version"] == batch.VERSION
    assert [call["to"] for call in params[0]["calls"]] == [TOKEN, POOL]


async def test_atomicity_is_asked_for_but_not_demanded():
    """An approval and the deposit after it do not have to land together to be
    worth batching, and demanding it turns away wallets that would do the
    sequence."""
    provider = Answering(wallet_sendCalls={"id": "0x1"})

    await batch.send(provider, ACCOUNT, MAINNET, [batch.Call(POOL)])

    assert provider.asked[0][1][0]["atomicRequired"] is False


async def test_a_value_bearing_call_carries_it_as_a_quantity():
    provider = Answering(wallet_sendCalls={"id": "0x1"})

    await batch.send(provider, ACCOUNT, MAINNET, [batch.Call(POOL, "0x", 10**18)])

    assert provider.asked[0][1][0]["calls"][0]["value"] == "0xde0b6b3a7640000"


async def test_the_draft_returned_the_id_as_a_bare_string():
    provider = Answering(wallet_sendCalls="0xbatch")

    assert await batch.send(provider, ACCOUNT, MAINNET, [batch.Call(POOL)]) == "0xbatch"


async def test_a_wallet_that_names_no_batch_is_refused_now_not_later():
    """Without an id there is nothing to follow up with, and finding that out
    at the status read would look like the batch had gone missing."""
    provider = Answering(wallet_sendCalls={"accepted": True})

    with pytest.raises(RpcError):
        await batch.send(provider, ACCOUNT, MAINNET, [batch.Call(POOL)])


async def test_an_empty_batch_is_a_mistake_worth_refusing():
    with pytest.raises(ValueError):
        await batch.send(Answering(), ACCOUNT, MAINNET, [])


# -- what became of it ------------------------------------------------------


def test_a_confirmed_batch_reports_its_transactions_and_block():
    got = batch.reads_as_batch({
        "status": 200,
        "atomic": True,
        "receipts": [
            {"transactionHash": "0xaa", "blockNumber": "0x10", "status": "0x1"},
        ],
    })

    assert not got.pending and not got.failed
    assert got.atomic and got.hashes == ("0xaa",) and got.block == 16


def test_a_batch_the_wallet_split_reports_every_transaction():
    """A wallet that batches without atomicity lands one transaction per call,
    and the block to re-read against is the last of them."""
    got = batch.reads_as_batch({
        "status": 200,
        "atomic": False,
        "receipts": [
            {"transactionHash": "0xaa", "blockNumber": "0x10", "status": "0x1"},
            {"transactionHash": "0xbb", "blockNumber": "0x11", "status": "0x1"},
        ],
    })

    assert got.hashes == ("0xaa", "0xbb") and got.block == 17


@pytest.mark.parametrize("code", [100, 0])
def test_a_batch_still_going_is_pending(code):
    assert batch.reads_as_batch({"status": code}).pending


@pytest.mark.parametrize("code", [400, 500, 600])
def test_a_code_past_confirmed_is_a_failure(code):
    got = batch.reads_as_batch({"status": code})

    assert not got.pending and got.failed


def test_a_confirmed_batch_holding_a_reverted_receipt_has_failed():
    """The draft had no code for a failure: a reverted call arrived as a
    confirmed batch whose receipt said zero, so that is where to read it."""
    got = batch.reads_as_batch({
        "status": "CONFIRMED",
        "receipts": [{"transactionHash": "0xaa", "blockNumber": "0x10",
                      "status": "0x0"}],
    })

    assert not got.pending and got.failed


def test_the_draft_spelling_of_pending():
    assert batch.reads_as_batch({"status": "PENDING"}).pending


def test_an_answer_nobody_recognises_is_pending_rather_than_failed():
    """Saying "it failed" from a parsing problem would be a claim about the
    chain that nothing checked.  The caller's timeout decides instead."""
    for answer in (None, "", {"status": "WHAT"}, {}):
        got = batch.reads_as_batch(answer)
        assert got.pending and not got.failed
