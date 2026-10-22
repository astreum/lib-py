from __future__ import annotations

from astreum.expression import Expr, int_, exprs_to_linked_expr
from astreum.communication.util import xor_distance


def _nearest(expr: Expr, source_key: bytes, target_key: bytes) -> bool:
    """True if *expr*'s hash is XOR-closer to *target_key* than to *source_key*."""
    expr_hash = expr.hash()
    return xor_distance(expr_hash, target_key) < xor_distance(expr_hash, source_key)


def generate_nearest_expr(
    source_key: bytes,
    target_key: bytes,
    *,
    value: int = 0,
) -> Expr:
    """Create an int atom whose hash is XOR-closest to *target_key*.

    Searches successive int atoms from *value* until the hash is strictly
    closer to *target_key* than to *source_key*, so DHT routing sends it to
    the target node.
    """
    for candidate in range(value, value + 100_000):
        expr = int_(candidate)
        if _nearest(expr, source_key, target_key):
            return expr
    raise RuntimeError("no expr nearest to target_key found")


def generate_nearest_expr_list(
    source_key: bytes,
    target_key: bytes,
    *,
    list_size: int = 4,
) -> Expr:
    """Create a link list whose hash is XOR-closest to *target_key*."""
    base = 0
    while base < 100_000:
        items = [int_(base + i) for i in range(list_size)]
        expr = exprs_to_linked_expr(items)
        if _nearest(expr, source_key, target_key):
            return expr
        base += 1
    raise RuntimeError("no list nearest to target_key found")
