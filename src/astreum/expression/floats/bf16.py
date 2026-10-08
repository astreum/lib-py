from math import isfinite
from struct import pack, unpack
from astreum.expression.floats.utils import _unpack_u16, _unpack_fp32

_BF16_TABLE = None

_BF16_MAX = 3.38953139e38


def _bf16_to_fp32(bf16: int) -> float:
    """Convert bf16 bits to fp64 via fp32 intermediate.
    
    Args:
        bf16: The bf16 bits.
    
    Returns:
        The fp64 float value.
    """
    fp32_bits = bf16 << 16
    return _unpack_fp32(pack('<I', fp32_bits))[0]


def _encode_bf16(value: float) -> bytes:
    """Encode fp64 to BF16 (16-bit brain float).
    
    Converts to fp32 first, then truncates mantissa to upper 16 bits.
    
    Args:
        value: The float value to encode.
    
    Returns:
        The encoded BF16 bytes.
    
    Raises:
        ValueError: If value overflows BF16 range.
    """
    if isfinite(value) and abs(value) > _BF16_MAX:
        raise ValueError("bf16 overflow")
    fp32_bits = unpack('<I', pack('<f', value))[0]
    bf16_bits = fp32_bits >> 16
    return pack('<H', bf16_bits)


def _decode_bf16(data: bytes) -> float:
    """Decode bf16 bytes to fp64 using LUT.
    
    Args:
        data: The bf16 bytes to decode.
    
    Returns:
        The decoded float value.
    """
    return _BF16_TABLE[_unpack_u16(data)[0]]


_BF16_TABLE = [_bf16_to_fp32(i) for i in range(65536)]


def bf16_(value: float):
    """Create a BF16 (16-bit brain float) expression.
    
    Args:
        value: The float value.
    
    Returns:
        A BF16 expression.
    """
    from astreum.expression.expr import Expr, BF16_SYMBOL
    return Expr("link", value=_encode_bf16(value), tail=BF16_SYMBOL)
