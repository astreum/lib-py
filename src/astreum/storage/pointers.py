"""Reading the pointers a cell holds to other cells.

A new cell points to an older cell with a stored ref
(``expression/refs.py``) and to a cell that is new in its block by hash. A
reader needs for each pointer the cell it names and the block whose provider
holds it: a ref names its block. A hash is in the block of the cell that holds
it. See ``docs/plans/2026-10-08-refs-in-tries.md``.
"""

from __future__ import annotations

from typing import Any, Optional

from astreum.expression import Expr, ZERO32, get_referenced_hash
from astreum.expression.refs import stored_ref_target
from astreum.storage.exprs import get_expr
from astreum.expression.helpers import get_expr_builtin_tag


def cell_reader(node: Any, block_hash: Optional[bytes], contact: Any = None):
    """Return a ``part(cell, side)`` function for reading cell heads/tails.

    The function returns the head or tail of *cell* as loaded, or fetched from
    the provider of *block_hash*. None for nil.

    Args:
        node: A Node instance for fetching expressions from storage.
        block_hash: The block whose provider holds the cell (None for local-only).
        contact: Optional ``RelayEndpoint`` for network fetches.

    Returns:
        A function ``part(cell, side)`` that returns the head or tail of a cell.
    """
    scope = {} if block_hash is None else {"block_hash": block_hash}

    def part(cell: Expr, side: str) -> Optional[Expr]:
        child = cell.head if side == "head" else cell.tail
        h = cell.head_hash if side == "head" else cell.tail_hash
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
    """Return ``(expr_id, block_id)`` for a pointed-to cell.

    For a child of a radix node: a stored ref names its target and block, any
    other cell is the target, in *block_hash*.

    Args:
        node: A Node instance for fetching expressions from storage.
        cell: The cell expression to read the pointer from.
        block_hash: The block that holds the cell (None for new cells).
        contact: Optional ``RelayEndpoint`` for network fetches.

    Returns:
        A tuple of ``(expr_id, block_id)`` where ``expr_id`` is the 32-byte
        content hash and ``block_id`` is the block hash (or None).
    """
    target = stored_ref_target(cell, _reader(node, block_hash, contact))
    if target is not None:
        return target[1], target[0]
    return cell.hash(), block_hash


def read_field(
    node: Any, cell: Expr, block_hash: Optional[bytes], contact: Any = None
) -> Optional[tuple[bytes, Optional[bytes]]]:
    """Return ``(expr_id, block_id)`` for a record field.

    The field may be a stored ref, or a wrapper ``Link(head_hash=expr_id)`` in
    *block_hash*. Returns None if *cell* is neither.

    Args:
        node: A Node instance for fetching expressions from storage.
        cell: The cell expression to read the field from.
        block_hash: The block that holds the record (None for new records).
        contact: Optional ``RelayEndpoint`` for network fetches.

    Returns:
        A tuple of ``(expr_id, block_id)`` or None if the cell is neither
        a stored ref nor a Link wrapper.
    """
    target = stored_ref_target(cell, _reader(node, block_hash, contact))
    if target is not None:
        return target[1], target[0]
    if get_expr_builtin_tag(cell) != "link":
        return None
    return get_referenced_hash(cell), block_hash
