"""ERC-20 call encoding, ABI decoding and EIP-55 checksums -- no dependencies.

`web3.py`/`eth-abi` would do all of this, but they drag in compiled
dependencies (`ckzg`, `pycryptodome`, ...) that make a Pyodide build either
impossible or enormous. The subset a "send a token" app actually needs is
about a hundred lines, so we write it, and the exact same code then runs on
CPython and on wasm32 with no build step.

Function selectors are hard-coded rather than derived, so keccak is only
needed for EIP-55 checksums.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# -- keccak-256 ------------------------------------------------------------
#
# Note this is original Keccak padding (0x01), not SHA-3's (0x06);
# hashlib.sha3_256 is a *different* hash and cannot be substituted here.

_MASK = (1 << 64) - 1

_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# _ROTATIONS[x][y] -- rho offsets.
_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_RATE = 136  # bytes absorbed per permutation for keccak-256


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(a: list[list[int]]) -> None:
    for rc in _ROUND_CONSTANTS:
        # theta
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        # rho + pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROTATIONS[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ (~b[(x + 1) % 5][y] & _MASK & b[(x + 2) % 5][y])
        # iota
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Ethereum's keccak-256."""
    state = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    for offset in range(0, len(padded), _RATE):
        block = padded[offset : offset + _RATE]
        for i in range(_RATE // 8):
            lane = int.from_bytes(block[i * 8 : i * 8 + 8], "little")
            state[i % 5][i // 5] ^= lane
        _keccak_f(state)

    out = bytearray()
    for i in range(4):  # 32 bytes = 4 lanes off the top of the state
        out += state[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out)


# -- addresses -------------------------------------------------------------

ZERO_ADDRESS = "0x" + "00" * 20


def is_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def to_checksum_address(value: str) -> str:
    """EIP-55 mixed-case form. Assumes `is_address(value)` already passed."""
    body = value[2:].lower()
    digest = keccak256(body.encode()).hex()
    return "0x" + "".join(
        ch.upper() if ch.isalpha() and int(digest[i], 16) >= 8 else ch
        for i, ch in enumerate(body)
    )


def is_checksum_address(value: str) -> bool:
    """True if the address carries a *valid* EIP-55 checksum.

    All-lower/all-upper addresses carry no checksum at all, so they are not
    "invalid" -- they are merely unverifiable. Callers should treat those as
    a warning, not an error.
    """
    return is_address(value) and value == to_checksum_address(value)


def has_checksum_case(value: str) -> bool:
    body = value[2:]
    return body != body.lower() and body != body.upper()


# -- ABI -------------------------------------------------------------------

# Hard-coded 4-byte selectors: keccak256("transfer(address,uint256)")[:4] etc.
SELECTOR_TRANSFER = "a9059cbb"
SELECTOR_BALANCE_OF = "70a08231"
SELECTOR_DECIMALS = "313ce567"
SELECTOR_SYMBOL = "95d89b41"
SELECTOR_NAME = "06fdde03"


def _encode_address(value: str) -> str:
    return value[2:].lower().rjust(64, "0")


def _encode_uint(value: int) -> str:
    if value < 0 or value >= 1 << 256:
        raise ValueError("uint256 out of range")
    return f"{value:064x}"


def encode_transfer(to: str, amount: int) -> str:
    """calldata for `transfer(address,uint256)`."""
    return "0x" + SELECTOR_TRANSFER + _encode_address(to) + _encode_uint(amount)


def encode_balance_of(owner: str) -> str:
    return "0x" + SELECTOR_BALANCE_OF + _encode_address(owner)


def encode_decimals() -> str:
    return "0x" + SELECTOR_DECIMALS


def encode_symbol() -> str:
    return "0x" + SELECTOR_SYMBOL


def encode_name() -> str:
    return "0x" + SELECTOR_NAME


def decode_uint(data: str) -> int:
    """Decode a single uint256 return value. Empty return data means 0."""
    raw = data[2:] if data.startswith("0x") else data
    return int(raw[:64], 16) if raw else 0


def decode_string(data: str) -> str:
    """Decode a string return value, tolerating the bytes32 variant.

    Tokens predating the finalised ERC-20 ABI (MKR is the famous one)
    declare `symbol()` as `bytes32`, which decodes as a right-padded raw
    string rather than the offset/length pair a real `string` uses. Sniff
    which one we got instead of trusting the ABI.
    """
    raw = data[2:] if data.startswith("0x") else data
    if not raw:
        return ""
    blob = bytes.fromhex(raw)
    if len(blob) == 32:  # bytes32: no offset header could fit
        return blob.rstrip(b"\x00").decode("utf-8", "replace")
    if len(blob) < 64:
        return blob.rstrip(b"\x00").decode("utf-8", "replace")
    offset = int.from_bytes(blob[:32], "big")
    if offset + 32 > len(blob):
        return blob[:32].rstrip(b"\x00").decode("utf-8", "replace")
    length = int.from_bytes(blob[offset : offset + 32], "big")
    body = blob[offset + 32 : offset + 32 + length]
    return body.decode("utf-8", "replace")


# -- amounts ---------------------------------------------------------------


def parse_units(value: str, decimals: int) -> int:
    """Human amount ("1.5") -> smallest units. Never touches float."""
    text = (value or "").strip().replace(",", "")
    if not text:
        raise ValueError("Enter an amount")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{value}' is not a number") from None
    if amount < 0:
        raise ValueError("Amount cannot be negative")
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Too many decimal places (max {decimals})")
    return int(scaled)


def format_units(value: int, decimals: int, precision: int = 6) -> str:
    """Smallest units -> a short human string, trailing zeros trimmed."""
    if decimals == 0:
        return str(value)
    quantised = Decimal(value) / (Decimal(10) ** decimals)
    text = f"{quantised:.{precision}f}".rstrip("0").rstrip(".")
    return text or "0"
