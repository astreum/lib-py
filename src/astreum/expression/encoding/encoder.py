from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.expression.expr import Expr

from astreum.expression.expr import (
    get_expr_head_hash,
    get_expr_tail_hash,
)


def encode_expr_to_bytes(expr: "Expr") -> bytes:
    """Encode an Expr to its byte representation.

    Args:
        expr: The expression to encode.

    Returns:
        The wire-format byte representation of the expression.

    Raises:
        TypeError: If expr is a closure (closures cannot be serialized).
    """
    if expr.base == "link":
        hh = get_expr_head_hash(expr)
        th = get_expr_tail_hash(expr)
        return b"\x00" + hh + th

    if expr.base == "symbol":
        val = expr.value.encode("utf-8")
        return b"\x01" + val

    if expr.base == "bytes":
        return b"\x02" + expr.value
