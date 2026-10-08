from math import isfinite
from struct import pack
from astreum.expression.floats.utils import _unpack_fp16

_FP16_MAX = 65504.0


def _encode_fp16(value: float) -> bytes:
    """Encode fp64 to IEEE 754 fp16 (16-bit) using struct.
    
    Args:
        value: The float value to encode.
    
    Returns:
        The encoded fp16 bytes.
    
    Raises:
        ValueError: If value overflows fp16 range.
    """
    if isfinite(value) and abs(value) > _FP16_MAX:
        raise ValueError("fp16 overflow")
    return pack('<e', value)


def _decode_fp16(data: bytes) -> float:
    """Decode fp16 bytes to fp64.
    
    Args:
        data: The fp16 bytes to decode.
    
    Returns:
        The decoded float value.
    """
    return _unpack_fp16(data)[0]


def fp16_(value: float):
    """Create an FP16 (16-bit float) expression.
    
    Args:
        value: The float value.
    
    Returns:
        An FP16 expression.
    """
    from astreum.expression.expr import Expr, FP16_SYMBOL
    return Expr("link", value=_encode_fp16(value), tail=FP16_SYMBOL)
