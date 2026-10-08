"""Shared pieces of ``ref`` and ``load``: both read an expr of an older block.

``(block_id expr_id ref)`` and ``(block_id expr_id load)`` pop ``expr_id``, then
``block_id``, and ask the provider of that block for the expr. A cell that is
itself a stored ref ``(block_id expr_id ref)``, as the system writes for older
cells in tries, is followed one level.
"""

from __future__ import annotations

from typing import Any, List, Optional

from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.expression import Expr, NIL, ZERO32, get_expr_tag
from astreum.machine import OpError
from astreum.storage.exprs import get_expr
from astreum.storage.radix import get_from_radix_tree

# One storage-trie read, charged per block check.
BLOCK_CHECK_BYTES = 32
# One cell fetched to recognise or follow a ref.
CELL_FETCH_BYTES = 70


def pop_scoped_args(machine, stack: List[Expr], name: str) -> tuple[bytes, bytes]:
    """Pop ``expr_id`` then ``block_id``; return ``(block_id, expr_id)``.

    There is no one-argument form: one value on the stack is an underflow.
    """
    if len(stack) < 2:
        machine.meter.charge_bytes(1)
        raise OpError("stack underflow")
    expr_id_expr = stack.pop()
    block_expr = stack.pop()

    tag = get_expr_tag(expr_id_expr)
    if tag != "bytes":
        raise OpError(f"{name} requires 32-byte hash, got {tag.lower()}")
    if len(expr_id_expr.value) != 32:
        raise OpError(f"{name} requires 32-byte hash, got {len(expr_id_expr.value)} bytes")
    tag = get_expr_tag(block_expr)
    if tag != "bytes":
        raise OpError(f"{name} requires 32-byte block id, got {tag.lower()}")
    if len(block_expr.value) != 32:
        raise OpError(f"{name} requires 32-byte block id, got {len(block_expr.value)} bytes")
    return block_expr.value, expr_id_expr.value


def check_block(machine, name: str, block_id: bytes) -> None:
    """Fail unless *block_id* is a key in the storage trie of the block being
    executed. The same on every validator, so an arbitrary 32 bytes cannot aim
    a validator's request at any provider.

    Without a block (execution outside consensus) there is no trie to check.
    """
    block = getattr(machine, "block", None)
    if block is None:
        return
    machine.meter.charge_bytes(BLOCK_CHECK_BYTES)
    storage_account = block.accounts.get_account(STORAGE_ADDRESS, machine.node)
    if (
        storage_account is None
        or get_from_radix_tree(storage_account.data, machine.node, block_id) is None
    ):
        raise OpError(f"{name}: unknown block")


def fetch(machine, name: str, block_id: bytes, expr_id: bytes) -> Expr:
    resolved = get_expr(machine.node, expr_id, block_hash=block_id)
    if resolved is None:
        raise OpError(f"{name}: expression not found")
    return resolved


def _is_nil(expr: Optional[Expr], h: Optional[bytes]) -> bool:
    if expr is None:
        return h is None or h == ZERO32
    return expr is NIL or (
        expr._tag == "link"
        and expr._head is None and expr._tail is None
        and expr._head_hash is None and expr._tail_hash is None
    )


def _part(machine, block_id: bytes, cell: Expr, side: str) -> Optional[Expr]:
    """The head or tail of *cell*: loaded, or fetched from *block_id* (a cell
    reached by plain hash is in the block of the cell that holds it)."""
    child = cell._head if side == "head" else cell._tail
    h = cell._head_hash if side == "head" else cell._tail_hash
    if child is not None:
        return child
    if h is None or h == ZERO32:
        return None
    machine.meter.charge_bytes(CELL_FETCH_BYTES)
    return get_expr(machine.node, h, block_hash=block_id)


def stored_ref_target(machine, block_id: bytes, cell: Expr) -> Optional[tuple[bytes, bytes]]:
    """``(block_id', expr_id')`` if *cell* is ``Link(bytes(B), Link(bytes(h),
    Link(symbol("ref"), NIL)))``, else None. Stops at the first part that does
    not fit, so an ordinary cell costs at most a couple of fetches."""
    if cell._tag != "link":
        return None
    block_leaf = _part(machine, block_id, cell, "head")
    if block_leaf is None or block_leaf._tag != "bytes" or len(block_leaf.value) != 32:
        return None
    second = _part(machine, block_id, cell, "tail")
    if second is None or second._tag != "link":
        return None
    id_leaf = _part(machine, block_id, second, "head")
    if id_leaf is None or id_leaf._tag != "bytes" or len(id_leaf.value) != 32:
        return None
    third = _part(machine, block_id, second, "tail")
    if third is None or third._tag != "link":
        return None
    marker = _part(machine, block_id, third, "head")
    if marker is None or marker._tag != "symbol" or marker.value != "ref":
        return None
    if not _is_nil(third._tail, third._tail_hash):
        return None
    return block_leaf.value, id_leaf.value


def follow_stored_ref(machine, name: str, block_id: bytes, cell: Expr) -> tuple[bytes, Expr]:
    """If *cell* is a stored ref, fetch what it names, from its block, and
    return that with the new block. One level: what comes back is not checked
    again, so a user ref to a ref is returned as data."""
    target = stored_ref_target(machine, block_id, cell)
    if target is None:
        return block_id, cell
    next_block, next_id = target
    check_block(machine, name, next_block)
    if next_id == ZERO32:
        return next_block, NIL
    return next_block, fetch(machine, name, next_block, next_id)
