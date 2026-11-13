"""Pure-Python Solana address helpers (no compiled deps): base58, the ed25519
on-curve test, and program-derived addresses (PDAs)."""
from __future__ import annotations

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ed25519 curve constants
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_IDX:
            raise ValueError(f"invalid base58 char {c!r}")
        n = n * 58 + _B58_IDX[c]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + body


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def is_on_curve(pub: bytes) -> bool:
    """True if 32 bytes decode to a valid ed25519 point (i.e. could be a wallet key)."""
    if len(pub) != 32:
        return False
    y = int.from_bytes(pub, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _P:
        return False
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return sign == 0
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    prog = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not is_on_curve(h):
            return b58encode(h), bump
    raise ValueError("no viable bump")


def pump_bonding_curve(mint: str) -> str:
    """pump.fun bonding-curve account for a mint: where its earliest buys happened."""
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)[0]


def looks_like_pump_mint(mint: str) -> bool:
    return mint.endswith("pump")
