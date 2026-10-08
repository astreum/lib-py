from typing import Optional


def hex_to_bytes(value: str, *, expected_length: Optional[int] = None) -> bytes:
    """Convert a 0x-prefixed hex string into raw bytes.

    Args:
        value: 0x-prefixed hex string to convert.
        expected_length: Optional expected byte length. If provided, validates
            the decoded bytes match this length.

    Returns:
        The decoded bytes.

    Raises:
        TypeError: If value is not a string.
        ValueError: If value does not start with '0x', has odd-length hex
            digits, contains non-hexadecimal characters, or does not match
            expected_length.
    """
    if not isinstance(value, str):
        raise TypeError("hex value must be provided as a string")

    if not value.startswith(("0x", "0X")):
        raise ValueError("hex value must start with '0x'")

    hex_digits = value[2:]
    if len(hex_digits) % 2:
        raise ValueError("hex value must have an even number of digits")

    try:
        result = bytes.fromhex(hex_digits)
    except ValueError as exc:
        raise ValueError("hex value contains non-hexadecimal characters") from exc

    if expected_length is not None and len(result) != expected_length:
        raise ValueError(f"hex value must decode to exactly {expected_length} bytes")

    return result
