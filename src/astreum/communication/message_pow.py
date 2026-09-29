from __future__ import annotations

from blake3 import blake3

NONCE_SIZE = 8
MAX_MESSAGE_NONCE = (1 << (NONCE_SIZE * 8)) - 1

# Max UDP payload for IPv4 datagrams.
MAX_UDP_DATAGRAM_BYTES = 65507

# Fixed per-message framing overhead on the wire: PoW nonce + type byte +
# 32-byte sender key + chacha nonce + timestamp (8, inside ciphertext) +
# topic byte (inside ciphertext) + poly1305 tag.  See Message.encrypt.
MESSAGE_FRAMING_BYTES = NONCE_SIZE + 1 + 32 + 12 + 8 + 1 + 16
MAX_INLINE_MESSAGE_BYTES = MAX_UDP_DATAGRAM_BYTES - MESSAGE_FRAMING_BYTES


def _leading_zero_bits(buf: bytes) -> int:
    """Return the number of leading zero bits in the provided buffer."""
    zeros = 0
    for byte in buf:
        if byte == 0:
            zeros += 8
            continue
        zeros += 8 - byte.bit_length()
        break
    return zeros


def calculate_message_nonce(message_bytes: bytes, difficulty: int) -> int:
    """Find a nonce such that blake3(message_bytes + nonce_bytes) meets difficulty.

    message_bytes should exclude any nonce prefix that will be added on the wire.
    """
    target = max(1, difficulty)
    nonce = 0
    message_bytes = message_bytes
    while True:
        if nonce > MAX_MESSAGE_NONCE:
            raise ValueError("nonce search exhausted")
        nonce_bytes = nonce.to_bytes(NONCE_SIZE, "big", signed=False)
        digest = blake3(message_bytes + nonce_bytes).digest()
        if _leading_zero_bits(digest) >= target:
            return nonce
        nonce += 1
