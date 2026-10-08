from math import isfinite

_FP64_MAX = 1.7976931348623157e308


def _encode_fp64(value: float) -> float:
    """Encode fp64 value with overflow check.
    
    Args:
        value: The float value.
    
    Returns:
        The validated float value.
    
    Raises:
        ValueError: If value overflows fp64 range.
    """
    if isfinite(value) and abs(value) > _FP64_MAX:
        raise ValueError("fp64 overflow")
    return value


def fp64_(value: float):
    """Create an FP64 (64-bit float) expression.
    
    Args:
        value: The float value.
    
    Returns:
        An FP64 expression.
    """
    from astreum.expression.expr import Expr, FP64_SYMBOL
    return Expr("link", value=_encode_fp64(value), tail=FP64_SYMBOL)
