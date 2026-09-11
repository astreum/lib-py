from __future__ import annotations

from struct import pack, unpack

from blake3 import blake3

from astreum.expression.floats import (
    e4m3_, e5m2_, fp16_, bf16_, fp32_, fp64_,
    FLOAT_TAGS, _ENCODE_FUNCS, _DECODE_FUNCS,
    _expr_to_fp64, _float_result, _float_to_bytes, _bytes_to_float_expr,
    _FLOAT_TAG_HASHES,
    HASH_SYMBOL_E4M3, HASH_SYMBOL_E5M2, HASH_SYMBOL_FP16,
    HASH_SYMBOL_BF16, HASH_SYMBOL_FP32, HASH_SYMBOL_FP64,
)

ZERO32 = b"\x00" * 32

RESOLUTION_SINGLE = 1
RESOLUTION_LIST = 2
RESOLUTION_FULL = 3
RESOLUTION_RECORD = 4

HASH_BYTE_LINK = b"\x00"
HASH_BYTE_SYMBOL = b"\x01"
HASH_BYTE_BYTES = b"\x02"


def _terminal_hash(tag_byte: bytes, data: bytes) -> bytes:
    return blake3(tag_byte + blake3(data).digest()).digest()


def _link_hash(head_hash: bytes, tail_hash: bytes) -> bytes:
    return blake3(HASH_BYTE_LINK + blake3(head_hash + tail_hash).digest()).digest()


def _encode_int(value: int) -> bytes:
    n = max(1, (value.bit_length() + 8) // 8)
    return value.to_bytes(n, "little", signed=True)


def _decode_int(data: bytes) -> int:
    return int.from_bytes(data, "little", signed=True)


# =============================================================================
# Tag encoding/decoding mappings
# =============================================================================

TAG_BYTE_ENCODINGS = {
    "int": _encode_int,
    "str": lambda v: v.encode("utf-8"),
    "symbol": lambda v: v.encode("utf-8"),
    "bytes": lambda v: v,
    "e4m3": lambda v: v,
    "e5m2": lambda v: v,
    "fp16": lambda v: v,
    "bf16": lambda v: v,
    "fp32": lambda v: v,
    "fp64": lambda v: pack('<d', v),
}

TAG_BYTE_DECODINGS = {
    "int": _decode_int,
    "str": lambda d: d.decode("utf-8"),
    "symbol": lambda d: d.decode("utf-8"),
    "bytes": lambda d: d,
    "e4m3": lambda d: d,
    "e5m2": lambda d: d,
    "fp16": lambda d: d,
    "bf16": lambda d: d,
    "fp32": lambda d: d,
    "fp64": lambda d: unpack('<d', d)[0],
}

TAG_SYMBOL_BYTES = {
    "int": b"int",
    "str": b"str",
    "symbol": b"symbol",
    "bytes": b"bytes",
    "e4m3": b"e4m3",
    "e5m2": b"e5m2",
    "fp16": b"fp16",
    "bf16": b"bf16",
    "fp32": b"fp32",
    "fp64": b"fp64",
}

# Precomputed hash for each builtin composite type symbol
HASH_SYMBOL_INT = _terminal_hash(HASH_BYTE_SYMBOL, b"int")
HASH_SYMBOL_STR = _terminal_hash(HASH_BYTE_SYMBOL, b"str")
HASH_SYMBOL_SYMBOL = _terminal_hash(HASH_BYTE_SYMBOL, b"symbol")
HASH_SYMBOL_BYTES = _terminal_hash(HASH_BYTE_SYMBOL, b"bytes")
# Float symbol hashes imported from floats module via _FLOAT_TAG_HASHES

# Subset of type names that are link-based builtin composites (not terminal wire types)
BUILTIN_COMPOSITE_TYPE_NAMES = {"int", "str"} | FLOAT_TAGS

# Reverse map: type-symbol hash → type name for immediate tag resolution
_BUILTIN_TYPE_HASH = {
    HASH_SYMBOL_INT: "int",
    HASH_SYMBOL_STR: "str",
    HASH_SYMBOL_SYMBOL: "symbol",
    HASH_SYMBOL_BYTES: "bytes",
    HASH_SYMBOL_E4M3: "e4m3",
    HASH_SYMBOL_E5M2: "e5m2",
    HASH_SYMBOL_FP16: "fp16",
    HASH_SYMBOL_BF16: "bf16",
    HASH_SYMBOL_FP32: "fp32",
    HASH_SYMBOL_FP64: "fp64",
}


def _get_head_hash(expr: Expr) -> bytes:
    if expr.head_hash is not None:
        return expr.head_hash
    if expr.value is not None and expr.tail is not None and expr.tail.base == "symbol":
        encoder = TAG_BYTE_ENCODINGS.get(expr.tail.value)
        if encoder is not None:
            return _terminal_hash(HASH_BYTE_BYTES, encoder(expr.value))
    if expr.head is NIL:
        return ZERO32
    if expr.head is not None:
        return expr.head.hash()
    return ZERO32


def _get_tail_hash(expr: Expr) -> bytes:
    if expr.tail_hash is not None:
        return expr.tail_hash
    if expr.tail is NIL:
        return ZERO32
    if expr.tail is not None:
        return expr.tail.hash()
    return ZERO32


class Expr:
    __slots__ = ("base", "value", "head", "tail", "head_hash", "tail_hash", "_hash", "_size")

    def __init__(self, base, value=None, head=None, tail=None, head_hash=None, tail_hash=None):
        self.base = base
        self.value = value
        self.head = head
        self.tail = tail
        self.head_hash = head_hash
        self.tail_hash = tail_hash
        self._hash = None
        self._size = None

    # --- backward-compat property aliases ---

    @property
    def _tag(self):
        if self.base in ("symbol", "bytes"):
            return self.base
        if self.base == "link":
            if self.tail is not None and self.tail.base == "symbol" and self.tail.value in BUILTIN_COMPOSITE_TYPE_NAMES:
                return self.tail.value
            return "link"
        return self.base

    @property
    def _value(self):
        return self.value

    @_value.setter
    def _value(self, v):
        self.value = v

    @property
    def _head(self):
        return self.head

    @_head.setter
    def _head(self, v):
        self.head = v

    @property
    def _tail(self):
        return self.tail

    @_tail.setter
    def _tail(self, v):
        self.tail = v

    @property
    def _head_hash(self):
        return self.head_hash

    @_head_hash.setter
    def _head_hash(self, v):
        self.head_hash = v

    @property
    def _tail_hash(self):
        return self.tail_hash

    @_tail_hash.setter
    def _tail_hash(self, v):
        self.tail_hash = v

    def __repr__(self):
        if self._tag == "int":
            return str(self._value)
        elif self._tag in FLOAT_TAGS:
            return str(_expr_to_fp64(self))
        elif self._tag == "str":
            return f'"{self._value}"'
        elif self._tag == "symbol":
            return self._value
        elif self._tag == "bytes":
            return "0x" + self._value.hex() if self._value else "0x"
        elif self._tag == "link":
            return self._repr_link()
        else:
            return f"#<{self._tag} {self._value}>"

    def _repr_link(self) -> str:
        if self is NIL:
            return "nil"

        if self._head_hash is not None and self.tail is None and self._tail_hash is None:
            return f"#{self._head_hash.hex()}"

        parts: list[str] = []
        current: Expr = self
        while current is not None and current.base == "link":
            if current._head_hash is not None:
                parts.append(f"#{current._head_hash.hex()}")
            elif current.head is not None:
                parts.append("nil" if current.head is NIL else repr(current.head))
            else:
                parts.append("nil")

            if current._tail_hash is not None:
                parts.append(f". #{current._tail_hash.hex()}")
                break
            if current.tail is NIL or current.tail is None:
                break
            if (current.tail.base == "link"
                    and current.tail._head_hash is None
                    and current.tail._tag == "link"):
                current = current.tail
                continue
            parts.append(f". {repr(current.tail)}")
            break

        return f"({' '.join(parts)})"

    def _hash_of_encoded(self) -> bytes:
        encoder = TAG_BYTE_ENCODINGS.get(self._tag)
        if encoder is None:
            if self._value is not None and hasattr(self._value, "hash"):
                return self._value.hash()
            return ZERO32
        return _terminal_hash(HASH_BYTE_BYTES, encoder(self._value))

    def hash(self):
        if self._hash is not None:
            return self._hash

        if self.base == "link":
            self._hash = _link_hash(_get_head_hash(self), _get_tail_hash(self))
            return self._hash

        if self.base == "symbol":
            self._hash = _terminal_hash(HASH_BYTE_SYMBOL, self.value.encode("utf-8"))
            return self._hash

        if self.base == "bytes":
            self._hash = _terminal_hash(HASH_BYTE_BYTES, self.value)
            return self._hash

        # Only reachable if base is unexpectedly wrong.
        head_hash = self._value.hash() if self._value is not None else ZERO32
        tail_hash = _terminal_hash(HASH_BYTE_SYMBOL, self.base.encode("utf-8"))
        self._hash = _link_hash(head_hash, tail_hash)
        return self._hash

    def size(self):
        if self._size is not None:
            return self._size

        if self.base == "link":
            # Builtin composite — use payload size
            if self.value is not None and self.tail is not None and self.tail.base == "symbol":
                encoder = TAG_BYTE_ENCODINGS.get(self.tail.value)
                if encoder is not None:
                    self._size = len(encoder(self.value))
                    return self._size
            if self.head is not None and self.head.base == "bytes":
                self._size = len(self.head.value)
                return self._size
            # Pair link
            h = self.head.size() if self.head is not None else 32
            if self.head_hash is not None:
                h = 32
            t = self.tail.size() if self.tail is not None else 32
            if self.tail_hash is not None:
                t = 32
            self._size = h + t
            return self._size

        if self.base == "symbol":
            self._size = len(self.value.encode("utf-8"))
            return self._size

        if self.base == "bytes":
            self._size = len(self.value)
            return self._size

        # Fallback for unexpected base values (legacy tag compatibility)
        encoder = TAG_BYTE_ENCODINGS.get(self.base)
        if encoder is not None:
            self._size = len(encoder(self._value))
            return self._size

        if self._value is not None:
            if hasattr(self._value, "size"):
                self._size = self._value.size()
                return self._size

        self._size = 0
        return 0

    def resolution(self) -> int:
        if self.base != "link":
            return RESOLUTION_SINGLE
        hh = self.head_hash
        if hh is None:
            hh = self.head.hash() if self.head is not None else ZERO32
        if hh == ZERO32:
            return RESOLUTION_LIST
        return RESOLUTION_FULL


# =============================================================================
# Type symbol singletons (pre-hashed)
# =============================================================================

INT_SYMBOL = Expr("symbol", value="int")
INT_SYMBOL._hash = HASH_SYMBOL_INT

STR_SYMBOL = Expr("symbol", value="str")
STR_SYMBOL._hash = HASH_SYMBOL_STR

E4M3_SYMBOL = Expr("symbol", value="e4m3")
E4M3_SYMBOL._hash = HASH_SYMBOL_E4M3

E5M2_SYMBOL = Expr("symbol", value="e5m2")
E5M2_SYMBOL._hash = HASH_SYMBOL_E5M2

FP16_SYMBOL = Expr("symbol", value="fp16")
FP16_SYMBOL._hash = HASH_SYMBOL_FP16

BF16_SYMBOL = Expr("symbol", value="bf16")
BF16_SYMBOL._hash = HASH_SYMBOL_BF16

FP32_SYMBOL = Expr("symbol", value="fp32")
FP32_SYMBOL._hash = HASH_SYMBOL_FP32

FP64_SYMBOL = Expr("symbol", value="fp64")
FP64_SYMBOL._hash = HASH_SYMBOL_FP64

# Mapping from type name to symbol singleton for runtime lookup
TYPE_SYMBOLS = {
    "int": INT_SYMBOL,
    "str": STR_SYMBOL,
    "e4m3": E4M3_SYMBOL,
    "e5m2": E5M2_SYMBOL,
    "fp16": FP16_SYMBOL,
    "bf16": BF16_SYMBOL,
    "fp32": FP32_SYMBOL,
    "fp64": FP64_SYMBOL,
}


# =============================================================================
# Constructor helpers
# =============================================================================

def int_(value: int) -> Expr:
    return Expr("link", value=value, tail=INT_SYMBOL)


def str_(value: str) -> Expr:
    return Expr("link", value=value, tail=STR_SYMBOL)


def symbol(value: str) -> Expr:
    return Expr("symbol", value=value)


def bytes_(value: bytes) -> Expr:
    return Expr("bytes", value=value)


def link(head, tail) -> Expr:
    return Expr("link", head=head, tail=tail)


# =============================================================================
# Utilities
# =============================================================================

def collect_list(expr: Expr) -> list:
    result = [expr]
    current = expr
    while current.base == "link" and current.tail is not None:
        current = current.tail
        result.append(current)
    return result


def collect_full(expr: Expr) -> list:
    result = []
    visited = set()

    def _walk(e):
        if e is None:
            return
        h = e.hash()
        if h in visited:
            return
        visited.add(h)
        result.append(e)
        if e.base == "link":
            _walk(e.head)
            _walk(e.tail)

    _walk(expr)
    return result


NIL = link(None, None)
