"""Reading the pointers a cell holds to other cells.

A new cell points to an older cell with a stored ref
(``expression/refs.py``) and to a cell that is new in its block by hash. A
reader needs for each pointer the cell it names and the block whose provider
holds it: a ref names its block; a hash is in the block of the cell that holds
it. See ``docs/plans/2026-10-08-refs-in-tries.md``.
"""

from __future__ import annotations

from typing import Any, Optional

from astreum.expression import Expr, ZERO32, get_referenced_hash
from astreum.expression.refs import stored_ref_target
from astreum.storage.exprs import get_expr


def cell_reader(node: Any, block_hash: Optional[bytes], contact: Any = None):
    """``part(cell, "head" | "tail")``: the head or tail of *cell* as loaded, or
    fetched from the provider of *block_hash*; None for nil. With no *node*
    nothing is fetched."""
    scope = {} if block_hash is None else {"block_hash": block_hash}

    def part(cell: Expr, side: str) -> Optional[Expr]:
        child = cell._head if side == "head" else cell._tail
        h = cell._head_hash if side == "head" else cell._tail_hash
        if child is not None:
            return child
        if h is None or h == ZERO32 or node is None:
            return None
        return get_expr(node, h, contact=contact, **scope)

    return part


_reader = cell_reader


def read_pointer(
    node: Any, cell: Expr, block_hash: Optional[bytes], contact: Any = None
) -> tuple[bytes, Optional[bytes]]:
    """``(expr_id, block_id)`` for an item that is the pointed-to cell itself
    (a child of a radix node): a stored ref names its target and block, any
    other cell is the target, in *block_hash*, the block that holds the item."""
    target = stored_ref_target(cell, _reader(node, block_hash, contact))
    if target is not None:
        return target[1], target[0]
    return cell.hash(), block_hash


def read_field(
    node: Any, cell: Expr, block_hash: Optional[bytes], contact: Any = None
) -> Optional[tuple[bytes, Optional[bytes]]]:
    """``(expr_id, block_id)`` for a field of a record: a stored ref, or a
    wrapper ``Link(head_hash=expr_id)`` whose cell is in *block_hash*, the
    block that holds the record. None if *cell* is neither."""
    target = stored_ref_target(cell, _reader(node, block_hash, contact))
    if target is not None:
        return target[1], target[0]
    if cell._tag != "link":
        return None
    return get_referenced_hash(cell), block_hash
