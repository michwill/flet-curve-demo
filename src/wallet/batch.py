"""EIP-5792: several calls handed to the wallet in one go.

An approval and the thing it is for are two transactions, and the second one
is worthless without the first.  Asking for them separately costs two wallet
prompts and, on a multisig, two rounds of cosigners -- which is why a Safe
connected over WalletConnect is the case this exists for.  A wallet that
implements 5792 takes the pair as one `wallet_sendCalls` and answers with a
batch id instead of a transaction hash.

**Two spellings of the same thing.**  The capability was `atomicBatch` with a
boolean while the EIP was a draft, and is `atomic` with a status of
`supported` or `ready` in the final text; the status codes were the strings
`PENDING`/`CONFIRMED` and are now the numbers 100/200/400/500.  Wallets in the
wild answer either, so both are read here and nothing downstream has to know.

**Not required.**  `atomicRequired` is false: an approval followed by a deposit
does not have to land in one transaction to be worth batching, and demanding
it turns away every wallet that will do the sequence but not the atomicity.
What actually happened comes back on `Batch.atomic`.

Nothing here reaches for a transport.  Every function takes a provider and
speaks to it through `request`, so this works on whichever wallet answered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import RpcError, WalletProvider

#: The version this app speaks.  A wallet on the draft ignores it and answers
#: in its own shape, which `Batch` reads either way.
VERSION = "2.0.0"

#: `wallet_getCallsStatus` codes, from the final text.
PENDING = 100
CONFIRMED = 200
#: Everything above 200 is over and did not work: 400 is offchain failure,
#: 500 is a reverted chain call, 600 is partial.
FAILED_FROM = 300

#: What a wallet answers when it has never heard of a method.  Not a constant
#: anyone agrees on -- some answer -32601, some -32000, some raise a plain
#: error -- so absence is inferred from *any* failure rather than from a code.
METHOD_NOT_FOUND = -32601


@dataclass(frozen=True, slots=True)
class Call:
    """One call in a batch: the same three fields a transaction carries."""

    to: str
    data: str = "0x"
    value: int = 0

    def as_json(self) -> dict[str, str]:
        """The wire shape.  `value` is a quantity, so hex and never padded."""
        return {"to": self.to, "data": self.data or "0x", "value": hex(self.value)}


@dataclass(frozen=True, slots=True)
class Batch:
    """What became of a batch, in the terms the app cares about."""

    pending: bool
    failed: bool
    atomic: bool = False
    receipts: tuple[dict[str, Any], ...] = field(default=())
    code: int = 0

    @property
    def hashes(self) -> tuple[str, ...]:
        """Every transaction the batch turned into, in order.

        One where the wallet was atomic, and one per call where it was not.
        """
        return tuple(
            receipt["transactionHash"]
            for receipt in self.receipts
            if receipt.get("transactionHash")
        )

    @property
    def block(self) -> int:
        """The last block the batch touched, or zero while it is pending."""
        blocks = [
            int(receipt["blockNumber"], 16)
            if isinstance(receipt.get("blockNumber"), str)
            else int(receipt.get("blockNumber") or 0)
            for receipt in self.receipts
        ]
        return max(blocks, default=0)


def _chain_key(chain_id: int) -> str:
    """How a chain is spelled in a capabilities answer: a bare hex quantity."""
    return hex(chain_id)


def reads_as_supported(capabilities: Any, chain_id: int) -> bool:
    """Whether this capabilities answer offers batching on this chain.

    Both spellings, and tolerant of a wallet that keys the chain differently
    -- `0x1` and `0x01` are the same chain and a capabilities map has been
    seen written either way.
    """
    if not isinstance(capabilities, dict):
        return False
    wanted = int(chain_id)
    for key, entry in capabilities.items():
        try:
            if int(str(key), 16) != wanted:
                continue
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        # The final text: "supported" means it always is, "ready" means it
        # would be after an upgrade the wallet is offering to do.  Both are
        # yes; "unsupported" is the third value and is not.
        atomic = entry.get("atomic")
        if isinstance(atomic, dict) and str(
                atomic.get("status", "")).lower() in ("supported", "ready"):
            return True
        legacy = entry.get("atomicBatch")
        if isinstance(legacy, dict) and legacy.get("supported"):
            return True
    return False


async def supported(provider: WalletProvider, account: str, chain_id: int) -> bool:
    """Whether this wallet will batch on this chain.

    Any failure is a no.  A wallet that has never heard of `wallet_getCapabilities`
    raises, and one that answers something unreadable is no better than one
    that refuses -- either way the two-step path is the one that works.
    """
    if not account:
        return False
    try:
        answer = await provider.request(
            "wallet_getCapabilities", [account, [_chain_key(chain_id)]]
        )
    except Exception:
        # Absence is the common case and arrives as any of several codes, or
        # as a transport error; none of them is worth telling apart from "no".
        return False
    return reads_as_supported(answer, chain_id)


async def send(
    provider: WalletProvider,
    account: str,
    chain_id: int,
    calls: list[Call],
    *,
    atomic: bool = False,
) -> str:
    """Hand the calls over. Returns the batch id to ask about afterwards."""
    if not calls:
        raise ValueError("a batch needs at least one call")
    answer = await provider.request(
        "wallet_sendCalls",
        [
            {
                "version": VERSION,
                "chainId": _chain_key(chain_id),
                "from": account,
                "atomicRequired": bool(atomic),
                "calls": [call.as_json() for call in calls],
            }
        ],
    )
    return _identifier(answer)


def _identifier(answer: Any) -> str:
    """The batch id, whichever shape it arrived in.

    The final text returns `{"id": ...}`; the draft returned the id as a bare
    string.  An answer with neither is a wallet this app cannot follow up
    with, which is worth saying now rather than when the status read fails.
    """
    if isinstance(answer, str) and answer:
        return answer
    if isinstance(answer, dict):
        found = answer.get("id") or answer.get("batchId")
        if isinstance(found, str) and found:
            return found
    raise RpcError(0, f"the wallet did not name the batch it accepted: {answer!r}")


async def status(provider: WalletProvider, batch_id: str) -> Batch:
    """Ask what became of a batch."""
    answer = await provider.request("wallet_getCallsStatus", [batch_id])
    return reads_as_batch(answer)


def reads_as_batch(answer: Any) -> Batch:
    """A status answer in this app's terms, in either spelling.

    An answer that cannot be read at all is treated as still pending rather
    than as a failure: the batch may well be fine, and the caller's own
    timeout is what decides when to stop waiting.  Saying "it failed" on a
    shape nobody recognised would be a claim about the chain made from a
    parsing problem.
    """
    if not isinstance(answer, dict):
        return Batch(pending=True, failed=False)
    receipts = tuple(r for r in (answer.get("receipts") or []) if isinstance(r, dict))
    atomic = bool(answer.get("atomic"))
    raw = answer.get("status")
    if isinstance(raw, str):
        # The draft: PENDING or CONFIRMED, with no code for a failure -- a
        # reverted call showed up as a confirmed batch holding a failed
        # receipt, so that is where a failure has to be read from.
        confirmed = raw.strip().upper() == "CONFIRMED"
        return Batch(
            pending=not confirmed,
            failed=confirmed and _any_reverted(receipts),
            atomic=atomic,
            receipts=receipts,
        )
    try:
        code = int(raw)  # type: ignore[arg-type]  # None is what the guard is for
    except (TypeError, ValueError):
        return Batch(pending=True, failed=False, atomic=atomic, receipts=receipts)
    if code <= PENDING:
        return Batch(pending=True, failed=False, atomic=atomic, receipts=receipts,
                     code=code)
    failed = code >= FAILED_FROM or _any_reverted(receipts)
    return Batch(pending=False, failed=failed, atomic=atomic, receipts=receipts,
                 code=code)


def _any_reverted(receipts: tuple[dict[str, Any], ...]) -> bool:
    """Whether any receipt in the batch says its call reverted."""
    for receipt in receipts:
        raw = receipt.get("status")
        if raw is None:
            continue
        try:
            if int(str(raw), 16) == 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


__all__ = ["Batch", "Call", "CONFIRMED", "FAILED_FROM", "PENDING", "VERSION",
           "reads_as_batch", "reads_as_supported", "send", "status", "supported"]
