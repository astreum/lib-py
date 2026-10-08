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

# ``part(cell, "head" | "tail")``: the loaded head or tail of *cell*, fetching
# it if only its hash is known; None if absent.
PartReader = Callable[[Expr, str], Optional[Expr]]


def ref_expr(block_id: bytes, expr_id: bytes) -> Expr:
    """``Link(bytes(block_id), Link(bytes(expr_id), Link(symbol("ref"), NIL)))``."""
    return link(bytes_(block_id), link(bytes_(expr_id), link(symbol("ref"), NIL)))


def pointer_item(expr_id: bytes, block_id: Optional[bytes]) -> Expr:
    """The cell that stands for a pointer to *expr_id* in a field of a new cell.

    A reference wrapper ``Link(head_hash=expr_id)`` for a cell that is new, a
    ref when it is held by the record of an older block *block_id*.
    """
    if block_id is None:
        return Expr("link", head_hash=expr_id)
    return ref_expr(block_id, expr_id)


def _is_nil(expr: Optional[Expr], h: Optional[bytes]) -> bool:
    if expr is None:
        return h is None or h == ZERO32
    return expr is NIL or (
        expr._tag == "link"
        and expr._head is None and expr._tail is None
        and expr._head_hash is None and expr._tail_hash is None
    )


def local_part(cell: Expr, side: str) -> Optional[Expr]:
    """The head or tail of *cell* if it is loaded, else None (no fetching)."""
    return cell._head if side == "head" else cell._tail


def stored_ref_target(cell: Expr, part: PartReader = local_part) -> Optional[tuple[bytes, bytes]]:
    """``(block_id, expr_id)`` if *cell* is a stored ref, else None.

    *part* reads the head or tail of a cell, fetching what is not loaded. It
    stops at the first part that does not fit, so an ordinary cell costs at
    most a couple of reads, and a cell whose tail is nil (a pointer wrapper)
    none.
    """
    if cell._tag != "link":
        return None
    if _is_nil(cell._tail, cell._tail_hash):
        return None
    block_leaf = part(cell, "head")
    if block_leaf is None or block_leaf._tag != "bytes" or len(block_leaf.value) != 32:
        return None
    second = part(cell, "tail")
    if second is None or second._tag != "link":
        return None
    id_leaf = part(second, "head")
    if id_leaf is None or id_leaf._tag != "bytes" or len(id_leaf.value) != 32:
        return None
    third = part(second, "tail")
    if third is None or third._tag != "link":
        return None
    marker = part(third, "head")
    if marker is None or marker._tag != "symbol" or marker.value != "ref":
        return None
    if not _is_nil(third._tail, third._tail_hash):
        return None
    return block_leaf.value, id_leaf.value
