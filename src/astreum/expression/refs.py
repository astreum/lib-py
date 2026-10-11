"""Stored refs: ``(block_id expr_id ref)`` in place of a pointer to an older cell.

A block's record holds the cells that are new in the block. Where a new cell
points to a cell that is older, the system writes this expression instead of
the bare hash: ``Link(bytes(block_id), Link(bytes(expr_id), Link(symbol("ref"),
NIL)))``. The block id names the provider that holds ``expr_id``, which a bare
hash cannot. See ``docs/plans/2026-10-08-refs-in-tries.md``.
"""

from __future__ import annotations

from typing import Callable, Optional

from astreum.expression.expr import Expr, NIL, ZERO32, bytes_, link, symbol
from astreum.expression.helpers import get_expr_builtin_tag

# ``part(cell, "head" | "tail")``: the loaded head or tail of *cell*, fetching
# it if only its hash is known; None if absent.
PartReader = Callable[[Expr, str], Optional[Expr]]


def ref_expr(block_id: bytes, expr_id: bytes) -> Expr:
    """``Link(bytes(block_id), Link(bytes(expr_id), Link(symbol("ref"), NIL)))``.
    
    Args:
        block_id: The block identifier bytes.
        expr_id: The expression identifier bytes.
    
    Returns:
        A ref expression linking to the specified block and expr.
    """
    return link(bytes_(block_id), link(bytes_(expr_id), link(symbol("ref"), NIL)))


def pointer_item(expr_id: bytes, block_id: Optional[bytes]) -> Expr:
    """The cell that stands for a pointer to *expr_id* in a field of a new cell.

    A reference wrapper ``Link(head_hash=expr_id)`` for a cell that is new, a
    ref when it is held by the record of an older block *block_id*.
    
    Args:
        expr_id: The expression identifier bytes.
        block_id: Optional block identifier bytes. None for new cells.
    
    Returns:
        A link expression for new cells, or a ref expression for older blocks.
    """
    if block_id is None:
        return Expr("link", head_hash=expr_id)
    return ref_expr(block_id, expr_id)


def _is_nil(expr: Optional[Expr], h: Optional[bytes]) -> bool:
    """Check if an expression or hash represents nil.
    
    Args:
        expr: Optional expression to check.
        h: Optional hash to check.
    
    Returns:
        True if the expression or hash represents nil.
    """
    if expr is None:
        return h is None or h == ZERO32
    return expr is NIL or (
        get_expr_builtin_tag(expr) == "link"
        and expr.head is None and expr.tail is None
        and expr.head_hash is None and expr.tail_hash is None
    )


def local_part(cell: Expr, side: str) -> Optional[Expr]:
    """The head or tail of *cell* if it is loaded, else None (no fetching).
    
    Args:
        cell: The expression cell to read.
        side: Either "head" or "tail".
    
    Returns:
        The head or tail expression if loaded, else None.
    """
    return cell.head if side == "head" else cell.tail


def stored_ref_target(cell: Expr, part: PartReader = local_part) -> Optional[tuple[bytes, bytes]]:
    """``(block_id, expr_id)`` if *cell* is a stored ref, else None.

    *part* reads the head or tail of a cell, fetching what is not loaded. It
    stops at the first part that does not fit, so an ordinary cell costs at
    most a couple of reads, and a cell whose tail is nil (a pointer wrapper)
    none.
    
    Args:
        cell: The expression cell to check.
        part: Function to read head or tail of a cell.
    
    Returns:
        Tuple of (block_id, expr_id) if cell is a stored ref, else None.
    """
    if get_expr_builtin_tag(cell) != "link":
        return None
    if _is_nil(cell.tail, cell.tail_hash):
        return None
    block_leaf = part(cell, "head")
    if block_leaf is None or get_expr_builtin_tag(block_leaf) != "bytes" or len(block_leaf.value) != 32:
        return None
    second = part(cell, "tail")
    if second is None or get_expr_builtin_tag(second) != "link":
        return None
    id_leaf = part(second, "head")
    if id_leaf is None or get_expr_builtin_tag(id_leaf) != "bytes" or len(id_leaf.value) != 32:
        return None
    third = part(second, "tail")
    if third is None or get_expr_builtin_tag(third) != "link":
        return None
    marker = part(third, "head")
    if marker is None or get_expr_builtin_tag(marker) != "symbol" or marker.value != "ref":
        return None
    if not _is_nil(third.tail, third.tail_hash):
        return None
    return block_leaf.value, id_leaf.value
