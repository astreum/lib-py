from __future__ import annotations


def _bit(buf: bytes, idx: int) -> bool:
    """Extract a single bit from a byte buffer.

    Args:
        buf: The byte buffer.
        idx: The bit index (0-indexed).

    Returns:
        The bit value as a boolean.
    """
    byte_i, offset = divmod(idx, 8)
    return ((buf[byte_i] >> (7 - offset)) & 1) == 1


def _match_prefix(
    prefix: bytes,
    prefix_len: int,
    key: bytes,
    key_bit_offset: int,
) -> bool:
    """Check if a prefix matches a key at a given offset.

    Args:
        prefix: The prefix bytes to match.
        prefix_len: The length of the prefix in bits.
        key: The key bytes to match against.
        key_bit_offset: The starting bit offset in the key.

    Returns:
        True if the prefix matches, False otherwise.
    """
    total_bits = len(key) * 8
    if key_bit_offset + prefix_len > total_bits:
        return False
    for i in range(prefix_len):
        if _bit(prefix, i) != _bit(key, key_bit_offset + i):
            return False
    return True


def _bit_slice(
    buf: bytes,
    start_bit: int,
    length: int
) -> tuple[bytes, int]:
    """Extract a slice of bits from a buffer.

    Args:
        buf: The byte buffer.
        start_bit: The starting bit index.
        length: The number of bits to extract.

    Returns:
        A tuple of (bytes, bit_length) for the extracted bits.
    """
    if length == 0:
        return b"", 0

    total = int.from_bytes(buf, "big")
    bits_in_buf = len(buf) * 8

    shift = bits_in_buf - (start_bit + length)
    slice_int = (total >> shift) & ((1 << length) - 1)

    pad = (8 - (length % 8)) % 8
    slice_int <<= pad
    byte_len = (length + 7) // 8
    return slice_int.to_bytes(byte_len, "big"), length
