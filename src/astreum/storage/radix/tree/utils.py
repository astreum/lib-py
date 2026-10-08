from __future__ import annotations


def _bits_from_payload(payload: bytes, bit_length: int) -> str:
    """Convert byte payload to a bit string of given length.

    Args:
        payload: The byte payload.
        bit_length: The number of bits to extract.

    Returns:
        A string of '0' and '1' characters.
    """
    if bit_length <= 0 or not payload:
        return ""
    bit_stream = "".join(f"{byte:08b}" for byte in payload)
    return bit_stream[:bit_length]


def _bits_to_bytes(bit_string: str) -> bytes:
    """Convert a bit string to bytes.

    Args:
        bit_string: A string of '0' and '1' characters.

    Returns:
        The byte representation of the bit string.
    """
    if not bit_string:
        return b""
    pad = (8 - (len(bit_string) % 8)) % 8
    bit_string = bit_string + ("0" * pad)
    byte_len = len(bit_string) // 8
    return int(bit_string, 2).to_bytes(byte_len, "big")
