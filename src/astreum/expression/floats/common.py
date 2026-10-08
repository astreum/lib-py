from struct import pack, unpack
from blake3 import blake3

from astreum.expression.floats.e4m3 import _encode_e4m3, _E4M3_TABLE
from astreum.expression.floats.e5m2 import _encode_e5m2, _E5M2_TABLE
from astreum.expression.floats.fp16 import _encode_fp16, _decode_fp16
from astreum.expression.floats.bf16 import _encode_bf16, _BF16_TABLE
from astreum.expression.floats.fp32 import _encode_fp32, _decode_fp32
from astreum.expression.floats.fp64 import _encode_fp64
from astreum.expression.floats.utils import _unpack_fp16, _unpack_fp32, _unpack_u16

HASH_BYTE_SYMBOL = b"\x01"


def _terminal_hash(tag_byte: bytes, data: bytes) -> bytes:
    """Compute hash for a terminal expression.
    
    Args:
        tag_byte: The type tag byte.
        data: The data to hash.
    
    Returns:
        The computed hash.
    """
    return blake3(tag_byte + blake3(data).digest()).digest()


FLOAT_TAGS = frozenset(["e4m3", "e5m2", "fp16", "bf16", "fp32", "fp64"])



_ENCODE_FUNCS = {
    "e4m3": _encode_e4m3,
    "e5m2": _encode_e5m2,
    "fp16": _encode_fp16,
    "bf16": _encode_bf16,
    "fp32": _encode_fp32,
    "fp64": _encode_fp64,
}


_DECODE_FUNCS = {
    "e4m3": lambda b: _E4M3_TABLE[b[0]],
    "e5m2": lambda b: _E5M2_TABLE[b[0]],
    "fp16": _decode_fp16,
    "bf16": lambda b: _BF16_TABLE[_unpack_u16(b)[0]],
    "fp32": _decode_fp32,
    "fp64": lambda x: x,
}


def _expr_to_fp64(expr) -> float:
    """Decode any float tag to fp64 for arithmetic/comparison.
    
    Args:
        expr: The float expression to decode.
    
    Returns:
        The decoded fp64 value.
    
    Raises:
        ValueError: If expr is not a float type.
    """
    tag = expr._tag
    if tag == "fp64":
        return expr._value
    if tag == "e4m3":
        return _E4M3_TABLE[expr._value[0]]
    if tag == "e5m2":
        return _E5M2_TABLE[expr._value[0]]
    if tag == "fp16":
        return _unpack_fp16(expr._value)[0]
    if tag == "bf16":
        return _BF16_TABLE[_unpack_u16(expr._value)[0]]
    if tag == "fp32":
        return _unpack_fp32(expr._value)[0]
    raise ValueError(f"Not a float type: {tag}")


def _float_result(tag: str, value: float):
    """Encode an fp64 computed value back to the same type.
    
    Args:
        tag: The target float type tag.
        value: The fp64 value to encode.
    
    Returns:
        An expression of the specified type.
    
    Raises:
        ValueError: If the value overflows the target type.
    """
    from astreum.expression.expr import Expr, TYPE_SYMBOLS
    if tag == "fp64":
        _ENCODE_FUNCS["fp64"](value)  # overflow check
        return Expr("link", value=value, tail=TYPE_SYMBOLS["fp64"])
    return Expr("link", value=_ENCODE_FUNCS[tag](value), tail=TYPE_SYMBOLS[tag])


def _float_to_bytes(tag: str, value) -> bytes:
    """Get wire bytes for a float expression.
    
    Args:
        tag: The float type tag.
        value: The float value.
    
    Returns:
        The wire bytes representation.
    """
    if tag == "fp64":
        return pack('<d', value)
    return value


def _bytes_to_float_expr(tag: str, data: bytes):
    """Create a float expression from wire bytes.
    
    Args:
        tag: The float type tag.
        data: The wire bytes.
    
    Returns:
        A float expression.
    """
    from astreum.expression.expr import Expr, TYPE_SYMBOLS
    if tag == "fp64":
        return Expr("link", value=unpack('<d', data)[0], tail=TYPE_SYMBOLS["fp64"])
    return Expr("link", value=data, tail=TYPE_SYMBOLS[tag])


HASH_SYMBOL_E4M3 = _terminal_hash(HASH_BYTE_SYMBOL, b"e4m3")
HASH_SYMBOL_E5M2 = _terminal_hash(HASH_BYTE_SYMBOL, b"e5m2")
HASH_SYMBOL_FP16 = _terminal_hash(HASH_BYTE_SYMBOL, b"fp16")
HASH_SYMBOL_BF16 = _terminal_hash(HASH_BYTE_SYMBOL, b"bf16")
HASH_SYMBOL_FP32 = _terminal_hash(HASH_BYTE_SYMBOL, b"fp32")
HASH_SYMBOL_FP64 = _terminal_hash(HASH_BYTE_SYMBOL, b"fp64")


_FLOAT_TAG_HASHES = {
    "e4m3": HASH_SYMBOL_E4M3,
    "e5m2": HASH_SYMBOL_E5M2,
    "fp16": HASH_SYMBOL_FP16,
    "bf16": HASH_SYMBOL_BF16,
    "fp32": HASH_SYMBOL_FP32,
    "fp64": HASH_SYMBOL_FP64,
}
