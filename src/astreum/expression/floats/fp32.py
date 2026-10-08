from math import isfinite
from struct import pack
from astreum.expression.floats.utils import _unpack_fp32

_FP32_MAX = 3.40282347e38


def _encode_fp32(value: float) -> bytes:
    """Encode fp64 to IEEE 754 fp32 (32-bit).
    
    Args:
        value: The float value to encode.
    
    Returns:
        The encoded fp32 bytes.
    
    Raises:
        ValueError: If value overflows fp32 range.
    """
    if isfinite(value) and abs(value) > _FP32_MAX:
        raise ValueError("fp32 overflow")
    return pack('<f', value)


def _decode_fp32(data: bytes) -> float:
    """Decode fp32 bytes to fp64.
    
    Args:
        data: The fp32 bytes to decode.
    
    Returns:
        The decoded float value.
    """
    return _unpack_fp32(data)[0]


def fp32_(value: float):
    """Create an FP32 (32-bit float) expression.
    
    Args:
        value: The float value.
    
    Returns:
        An FP32 expression.
    """
    from astreum.expression.expr import Expr, FP32_SYMBOL
    return Expr("link", value=_encode_fp32(value), tail=FP32_SYMBOL)
