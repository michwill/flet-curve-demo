"""Asking a chain several questions in one round trip.

Reading a pool's parameters is eleven calls, and a twelfth and thirteenth
for the two that have a second spelling. Sent one at a time against a
public endpoint on the other side of the world, that is thirteen round
trips for a panel nobody wants to wait for -- and thirteen chances to be
rate-limited.

**Multicall3** collapses them into one `eth_call`. It is at
`0xcA11bde05977b3631167028862bE2a173976CA11` on every chain that has it,
because it was deployed with a deterministic-deployment proxy from the
same signed transaction: same bytecode, same address, ~200 chains. So
there is nothing to look up and nothing to configure.

`aggregate3` is the variant to use, because it takes `allowFailure` per
call. That matters here more than it sounds: asking a StableSwap pool for
`gamma()` is *expected* to fail, and with `aggregate` (no failures
allowed) one such call would take the whole batch down with it. With
`aggregate3` the batch comes back with a success flag per call, which is
exactly the "ask everything, show what answered" shape the caller wants.

**A chain without Multicall3 is a normal case**, not an error -- a Curve
Lite deployment on a new chain may well predate it. The encoding here has
no way to tell that apart from any other failed call, so callers treat
*any* failure of the batch as "no multicall here" and fall back to asking
one at a time. That is why nothing in this module raises.

ABI note, since this is the most involved encoding in the app: the
argument is a dynamic array of structs that each contain dynamic `bytes`.
So there are three levels of offset -- one to the array, one per element,
and one to each element's `bytes` -- and each is measured from a
different base. The tests pin a known-good encoding against the real
contract.
"""

from __future__ import annotations

from .abi import _address, _bool, _offset, _uint, decode_uint, selector

#: Same address on every chain that has it. Deployed from a presigned
#: transaction through the deterministic-deployment proxy, which is why it
#: needs no per-chain table.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

#: `aggregate3` rather than `aggregate`: it allows a call to fail without
#: reverting the batch, and half the calls here are expected to fail.
AGGREGATE3 = "aggregate3((address,bool,bytes)[])"


def _bytes_tail(data: str) -> str:
    """A `bytes` value: length in bytes, then the data padded to a word."""
    body = data[2:] if data.startswith("0x") else data
    if len(body) % 2:
        raise ValueError("odd-length calldata")
    padding = (-len(body)) % 64
    return _uint(len(body) // 2) + body + "0" * padding


def encode_aggregate3(
    calls: list[tuple[str, str]], *, allow_failure: bool = True
) -> str:
    """Calldata for `aggregate3`, from `(target, calldata)` pairs.

    `allowFailure` defaults to true, because the usual caller here is
    asking which of these a contract implements and a revert is one of the
    answers.

    **A write must pass false.** With failures allowed the outer
    transaction succeeds whatever the inner calls did, so a gauge that
    refuses to pay comes back as a mined transaction and a green line,
    with the reason discarded -- and that is not a hypothetical. It is how
    this app came to claim, on the record, that `claim_rewards(address)`
    could not be batched: the batch was sent, `aggregate3` swallowed the
    inner result, and the absence of tokens was read as a refusal.
    """
    elements = [
        _address(target) + _bool(allow_failure) + _offset(3) + _bytes_tail(data)
        for target, data in calls
    ]
    # Each element's offset is measured from the start of the element
    # area -- after the array's own length word, not from the start of
    # the arguments.
    heads: list[str] = []
    position = len(elements) * 32
    for element in elements:
        heads.append(_uint(position))
        position += len(element) // 2
    return (
        "0x"
        + selector(AGGREGATE3)
        + _offset(1)  # the one argument is dynamic: point past this word
        + _uint(len(elements))
        + "".join(heads)
        + "".join(elements)
    )


def decode_aggregate3(result: str) -> list[str | None]:
    """`(bool success, bytes returnData)[]` -> the data, or None per call.

    None means that call failed or answered nothing, which for this app's
    purposes are the same thing: a Curve pool that does not implement a
    method returns empty data rather than reverting, and neither is a
    value.

    Returns an empty list for anything that cannot be read as this shape,
    so a chain without Multicall3 -- where the call hits an address with
    no code and comes back `0x` -- is simply "no answers" and the caller
    falls back.
    """
    body = (result or "")[2:] if (result or "").startswith("0x") else (result or "")
    words = [body[i : i + 64] for i in range(0, len(body), 64)]
    if len(words) < 2:
        return []
    try:
        array_at = int(words[0], 16) // 32
        count = int(words[array_at], 16)
    except (ValueError, IndexError, OverflowError):
        # Junk, or an offset that points outside the answer. Both mean the
        # same thing to the caller: ask one at a time.
        return []
    if count > len(words):
        return []
    base = array_at + 1
    if count == 0 or len(words) < base + count:
        return []

    answers: list[str | None] = []
    for index in range(count):
        try:
            element = base + int(words[base + index], 16) // 32
            success = int(words[element], 16) == 1
            data_at = element + int(words[element + 1], 16) // 32
            length = int(words[data_at], 16)
        except (ValueError, IndexError):
            answers.append(None)
            continue
        if not success or length == 0:
            answers.append(None)
            continue
        chunk = "".join(words[data_at + 1 :])[: length * 2]
        answers.append("0x" + chunk)
    return answers


def decode_uints(result: str, count: int) -> list[int | None]:
    """The common case: every call returns one `uint256`, or nothing."""
    answers = decode_aggregate3(result)
    if len(answers) != count:
        return [None] * count
    return [decode_uint(answer) if answer else None for answer in answers]
