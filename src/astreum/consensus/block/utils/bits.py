def count_leading_zero_bits(buf: bytes) -> int:
    """Count leading zero bits in a byte string (big-endian)."""

    Args:
        buf (bytes): Description.

    zeros = 0
    for byte in buf:
        if byte == 0:
            zeros += 8
            continue
        zeros += 8 - byte.bit_length()
        break
    return zeros
