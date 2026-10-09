from __future__ import annotations

from struct import unpack

from astreum.expression.expr import (
    Expr, NIL, ZERO32, bytes_, link,
    _decode_int,
    BUILTIN_COMPOSITE_TYPE_NAMES,
)
from astreum.expression.floats.common import FLOAT_TAGS
from astreum.expression.floats.fp16 import _decode_fp16
from astreum.expression.floats.bf16 import _decode_bf16


def _scope(block_hash) -> dict:
    """Create scope dict for fetch operations.
    
    Args:
        block_hash: Optional block hash to scope fetches.
    
    Returns:
        Empty dict if block_hash is None, else dict with block_hash.
    """
    return {} if block_hash is None else {"block_hash": block_hash}


def is_builtin_composite(expr: Expr) -> bool:
    """Check if expr is a builtin composite (e.g. int, str, float).

    A link is a builtin composite iff:
    - base == "link"
    - it has a payload source (value holds data, or head is a bytes expr)
    - tail is a symbol whose value is a known builtin composite type
    
    Args:
        expr: The expression to check.
    
    Returns:
        True if the expression is a builtin composite.
    """
    if expr.base != "link":
        return False
    if expr.value is None and (expr.head is None or expr.head.base != "bytes"):
        return False
    if expr.tail is None or expr.tail.base != "symbol":
        return False
    return expr.tail.value in BUILTIN_COMPOSITE_TYPE_NAMES


def get_referenced_hash(expr: Expr) -> bytes:
    """The hash an ``Expr("link", head_hash=H)`` reference points to.

    Still ``H`` when the reference has been resolved in place (``head`` set,
    ``head_hash`` cleared), and ``ZERO32`` for a reference to nil. This is the
    hash of the *head*, not of the reference expr itself.
    
    Args:
        expr: The link expression to get the referenced hash from.
    
    Returns:
        The hash of the referenced expression.
    """
    if expr._head_hash is not None:
        return expr._head_hash
    if expr._head is not None:
        return expr._head.hash()
    return ZERO32


def get_expr_tag(expr: Expr, node=None, contact=None, block_hash=None):
    """Get the logical type tag of an expression.

    Base types (symbol, bytes) return their base.
    Links with a symbol tail return the symbol\'s value (e.g. "int", "ok", "lex").
    Links with an unresolved tail_hash matching a builtin type hash return the type name.
    Links with a custom unresolved tail_hash raise if node is None, else resolve and retry.
    Plain links (tail is not a symbol) return "link".
    *contact* and *block_hash* (the block that holds the cell) are passed to the
    fetch that resolves a custom tail_hash.
    
    Args:
        expr: The expression to get the tag from.
        node: Optional node for resolving unresolved hashes.
        contact: Optional contact for fetch operations.
        block_hash: Optional block hash that holds the cell.
    
    Returns:
        The logical type tag of the expression.
    
    Raises:
        ValueError: If tag is unresolved and node is None.
    """
    if expr.base in ("symbol", "bytes"):
        return expr.base
    if expr.base == "link":
        if expr.tail is not None:
            if expr.tail.base == "symbol":
                return expr.tail.value
            return "link"
        if expr.tail_hash is not None and expr.tail_hash != ZERO32:
            from astreum.expression.expr import _BUILTIN_TYPE_HASH
            name = _BUILTIN_TYPE_HASH.get(expr.tail_hash)
            if name is not None:
                return name
            if node is not None:
                from astreum.storage.exprs import get_expr
                resolved = get_expr(
                    node, expr.tail_hash, contact=contact, **_scope(block_hash)
                )
                if resolved is not None:
                    expr.tail = resolved
                    expr.tail_hash = None
                    if expr.tail.base == "symbol":
                        return expr.tail.value
                    return "link"
            raise ValueError("tag unresolved: pass node to resolve custom tail_hash")
        return "link"
    return expr.base


def get_expr_value(expr, node=None, contact=None, block_hash=None):
    """Get the payload value of an expression.

    For builtin composite links (int, str, float), resolves head_hash if needed.
    For bytes/symbol terminals, returns the value directly.
    For pair links, returns the head\'s value if it\'s a bytes expr, else raises.
    *contact* and *block_hash* (the block that holds the cell) are passed to the
    fetches that resolve head_hash and the tag.
    
    Args:
        expr: The expression to get the value from.
        node: Optional node for resolving unresolved hashes.
        contact: Optional contact for fetch operations.
        block_hash: Optional block hash that holds the cell.
    
    Returns:
        The payload value of the expression.
    
    Raises:
        ValueError: If value requires node to resolve head_hash or cannot resolve.
    """
    if expr.value is not None:
        return expr.value
    if expr.base == "link":
        if expr.head is not None:
            # Already resolved (possibly by an earlier read of an identical,
            # shared expr): decode exactly as the first read would, so the
            # result does not depend on how many times it was read.
            head = expr.head
            if head.base == "bytes":
                from astreum.expression.expr import TAG_BYTE_DECODINGS
                try:
                    tag = get_expr_tag(
                        expr, node, contact=contact, block_hash=block_hash
                    )
                except ValueError:
                    tag = None
                if tag in TAG_BYTE_DECODINGS:
                    return TAG_BYTE_DECODINGS[tag](head.value)
            return head.value
        if expr.head is None and expr.head_hash is not None and expr.head_hash != ZERO32:
            if node is None:
                raise ValueError("value requires node to resolve head_hash")
            from astreum.storage.exprs import get_expr
            from astreum.expression.expr import TAG_BYTE_DECODINGS
            head = get_expr(node, expr.head_hash, contact=contact, **_scope(block_hash))
            if head is None:
                raise ValueError("cannot resolve head for expr value")
            expr.head = head
            expr.head_hash = None
            tag = get_expr_tag(expr, node, contact=contact, block_hash=block_hash)
            if head.base == "bytes" and tag in TAG_BYTE_DECODINGS:
                return TAG_BYTE_DECODINGS[tag](head.value)
            return head.value
    raise ValueError("no value")


def get_int_from_expr(expr: Expr) -> int:
    """Get an integer value from an expression.
    
    Args:
        expr: The expression to get the integer from.
    
    Returns:
        The integer value.
    """
    val = get_expr_value(expr)
    if isinstance(val, int):
        return val
    return _decode_int(val)


def get_str_from_expr(expr: Expr) -> str:
    """Get a string value from an expression.
    
    Args:
        expr: The expression to get the string from.
    
    Returns:
        The string value.
    """
    val = get_expr_value(expr)
    if isinstance(val, str):
        return val
    return val.decode("utf-8")


def get_symbol_from_expr(expr: Expr) -> str:
    """Get a symbol value from an expression.
    
    Args:
        expr: The expression to get the symbol from.
    
    Returns:
        The symbol value as a string.
    """
    return get_str_from_expr(expr)


def get_bytes_from_expr(expr: Expr) -> bytes:
    """Get a bytes value from an expression.
    
    Args:
        expr: The expression to get the bytes from.
    
    Returns:
        The bytes value.
    """
    return get_expr_value(expr)


def get_e4m3_from_expr(expr: Expr) -> bytes:
    """Get an E4M3 float value from an expression.
    
    Args:
        expr: The expression to get the E4M3 value from.
    
    Returns:
        The E4M3 bytes value.
    """
    return get_expr_value(expr)


def get_e5m2_from_expr(expr: Expr) -> bytes:
    """Get an E5M2 float value from an expression.
    
    Args:
        expr: The expression to get the E5M2 value from.
    
    Returns:
        The E5M2 bytes value.
    """
    return get_expr_value(expr)


def get_fp16_from_expr(expr: Expr) -> float:
    """Get an FP16 float value from an expression.
    
    Args:
        expr: The expression to get the FP16 value from.
    
    Returns:
        The FP16 float value.
    """
    val = get_expr_value(expr)
    if isinstance(val, float):
        return val
    return _decode_fp16(val)


def get_bf16_from_expr(expr: Expr) -> float:
    """Get a BF16 float value from an expression.
    
    Args:
        expr: The expression to get the BF16 value from.
    
    Returns:
        The BF16 float value.
    """
    val = get_expr_value(expr)
    if isinstance(val, float):
        return val
    return _decode_bf16(val)


def get_fp32_from_expr(expr: Expr) -> float:
    """Get an FP32 float value from an expression.
    
    Args:
        expr: The expression to get the FP32 value from.
    
    Returns:
        The FP32 float value.
    """
    val = get_expr_value(expr)
    if isinstance(val, float):
        return val
    return unpack('<f', val)[0]


def get_fp64_from_expr(expr: Expr) -> float:
    """Get an FP64 float value from an expression.
    
    Args:
        expr: The expression to get the FP64 value from.
    
    Returns:
        The FP64 float value.
    """
    val = get_expr_value(expr)
    if isinstance(val, float):
        return val
    return unpack('<d', val)[0]


def bytes_list_to_expr(items: list[bytes]) -> Expr:
    """Convert a list of bytes to a linked expression.
    
    Args:
        items: List of bytes values.
    
    Returns:
        A linked expression of bytes values.
    """
    if not items:
        return NIL
    result: Expr = NIL
    for value in reversed(items):
        result = link(bytes_(value), result)
    return result


def link_list_to_expr(items: list[bytes]) -> Expr:
    """Convert a list of hashes to a linked expression.
    
    Args:
        items: List of hash bytes.
    
    Returns:
        A linked expression of hash references.
    """
    if not items:
        return NIL
    head = Expr("link", head_hash=items[0], tail=NIL)
    current = head
    for value in items[1:]:
        new_link = Expr("link", head_hash=value, tail=NIL)
        current.tail = new_link
        current = new_link
    return head


def exprs_to_linked_expr(items: list[Expr]) -> Expr:
    """Convert a list of expressions to a linked expression.
    
    Args:
        items: List of expressions.
    
    Returns:
        A linked expression of the items.
    """
    if not items:
        return NIL
    result: Expr = NIL
    for item in reversed(items):
        result = link(item, result)
    return result


def resolve_list_exprs(
    node, expr: Expr, contact=None, block_hash=None
) -> tuple[list[Expr], list[bytes]]:
    """Resolve the items of a list. *block_hash* scopes the fetches to a block.
    
    Args:
        node: The node for resolving expressions.
        expr: The list expression to resolve.
        contact: Optional contact for fetch operations.
        block_hash: Optional block hash to scope fetches.
    
    Returns:
        Tuple of (resolved expressions, missed hashes).
    """
    from astreum.storage.exprs import get_expr

    scope = {} if block_hash is None else {"block_hash": block_hash}

    result: list[Expr] = []
    missed: list[bytes] = []
    current = expr
    while current is not None and current.base == "link":
        if current.head is None and current.value is None and current.head_hash is not None:
            if current.head_hash == ZERO32:
                current.head = NIL
                current.head_hash = None
            else:
                resolved = get_expr(node, current.head_hash, contact=contact, **scope)
                if resolved is not None:
                    current.head = resolved
                    current.head_hash = None
                else:
                    missed.append(current.head_hash)
        if current.head is not None:
            result.append(current.head)
        if current.tail is None and current.tail_hash is not None:
            if current.tail_hash == ZERO32:
                current.tail = None
                current.tail_hash = None
            else:
                resolved = get_expr(node, current.tail_hash, contact=contact, **scope)
                if resolved is not None:
                    current.tail = resolved
                    current.tail_hash = None
                else:
                    missed.append(current.tail_hash)
                    break
        current = current.tail
    return result, missed


def resolve_inner_exprs(
    node, expr: Expr, contact=None, block_hash=None
) -> tuple[list[Expr], list[bytes]]:
    """Resolve every inner expr. *block_hash* scopes the fetches to a block.
    
    Args:
        node: The node for resolving expressions.
        expr: The expression to resolve.
        contact: Optional contact for fetch operations.
        block_hash: Optional block hash to scope fetches.
    
    Returns:
        Tuple of (resolved expressions, missed hashes).
    """
    from astreum.storage.exprs import get_expr

    scope = {} if block_hash is None else {"block_hash": block_hash}

    result: list[Expr] = []
    missed: list[bytes] = []

    def _walk(e: Expr) -> None:
        result.append(e)
        if e.base != "link":
            return
        if e.head is None and e.value is None and e.head_hash is not None:
            resolved = get_expr(node, e.head_hash, contact=contact, **scope)
            if resolved is not None:
                e.head = resolved
                e.head_hash = None
            else:
                missed.append(e.head_hash)
        if e.head is not None:
            _walk(e.head)
        if e.tail is None and e.tail_hash is not None:
            resolved = get_expr(node, e.tail_hash, contact=contact, **scope)
            if resolved is not None:
                e.tail = resolved
                e.tail_hash = None
            else:
                missed.append(e.tail_hash)
                return
        if e.tail is not None:
            _walk(e.tail)

    _walk(expr)
    return result, missed
