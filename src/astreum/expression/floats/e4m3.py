from math import isnan, isinf, isfinite, log2, floor

_E4M3_TABLE = None

def _e4m3_to_fp64(b: int) -> float:
    """Decode E4M3 (4-bit exponent, 3-bit mantissa, 8-bit float) to fp64.
    
    Format: s eeee mmm
    - s: sign bit (bit 7)
    - eeee: 4-bit exponent (bits 6-3), biased by 7
    - mmm: 3-bit mantissa (bits 2-0)
    - Exponent 0b0000: zero or subnormal
    - Exponent 0b1111: infinity or NaN
    - Two NaN encodings: 0b01111111 and 0b11111111
    
    Args:
        b: The 8-bit E4M3 value.
    
    Returns:
        The decoded float value.
    """
    sign = -1.0 if (b & 0x80) else 1.0
    exp = (b >> 3) & 0x0F
    mantissa = b & 0x07
    
    if exp == 0:
        if mantissa == 0:
            return 0.0 if (b & 0x80) == 0 else -0.0
        return sign * (2 ** -6) * (mantissa / 8.0)
    elif exp == 0x0F:
        return float('nan')
    else:
        return sign * (2 ** (exp - 7)) * (1.0 + mantissa / 8.0)


def _encode_e4m3(value: float) -> bytes:
    """Encode fp64 to E4M3 (8-bit).
    
    Args:
        value: The float value to encode.
    
    Returns:
        The encoded E4M3 bytes.
    
    Raises:
        ValueError: If value overflows E4M3 range.
    """
    if isnan(value):
        return b'\x7f'
    if isinf(value):
        return b'\x7e' if value > 0 else b'\xfe'
    
    sign = 0x80 if value < 0 else 0x00
    abs_val = abs(value)
    
    if abs_val == 0.0:
        return bytes([sign])
    
    if isfinite(value) and abs_val > 448.0:
        raise ValueError("e4m3 overflow")
    
    logv = log2(abs_val)
    exp_unbiased = int(floor(logv)) + 7
    exp_unbiased = max(1, min(exp_unbiased, 14))
    
    mantissa_val = abs_val / (2 ** (exp_unbiased - 7)) - 1.0
    mantissa = int(round(mantissa_val * 8))
    if mantissa >= 8:
        mantissa = 7
    
    encoded = sign | (exp_unbiased << 3) | mantissa
    
    decoded = _e4m3_to_fp64(encoded)
    if mantissa < 7:
        alt_encoded = sign | (exp_unbiased << 3) | (mantissa ^ 1)
        alt_decoded = _e4m3_to_fp64(alt_encoded)
        if abs(alt_decoded - abs_val) < abs(decoded - abs_val):
            encoded = alt_encoded
    
    return bytes([encoded])


_E4M3_TABLE = [_e4m3_to_fp64(i) for i in range(256)]


def e4m3_(value: float):
    """Create an E4M3 (8-bit float) expression.
    
    Args:
        value: The float value.
    
    Returns:
        An E4M3 expression.
    """
    from astreum.expression.expr import Expr, E4M3_SYMBOL
    return Expr("link", value=_encode_e4m3(value), tail=E4M3_SYMBOL)
