from __future__ import annotations

from typing import Any, Iterable, List, Optional, Tuple

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.expression import Expr, resolve_inner_exprs, resolve_list_exprs
from astreum.expression import ZERO32, is_builtin_composite
from astreum.expression.expr import (
    BUILTIN_COMPOSITE_TYPE_NAMES,
    TAG_BYTE_ENCODINGS,
    _BUILTIN_TYPE_HASH,
    _get_head_hash,
    _get_tail_hash,
)
from astreum.storage.exprs import get_expr, get_expr_list
from astreum.storage.radix import RadixTree, get_from_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.crypto.merkle import create_merkle_tree

class UnresolvedRecordExpr(Exception):
    """An expr the record walk had to follow could not be fetched."""

    def __init__(self, expr_id: bytes):
        super().__init__(f"unable to resolve expr {expr_id.hex()} for storage record")
        self.expr_id = expr_id


def block_scope(block: object) -> bytes | None:
    """Return the block whose record holds the transaction's cells.

    Returns its id once it has one (a verified block), else None (one being built).

    Args:
        block: The block.

    Returns:
        The block id or None.
    """
    block_id = getattr(block, "expr_id", None)
    if not block_id or block_id == ZERO32:
        return None
    return block_id


def _scope(block_hash: bytes | None) -> dict:
    """Build scope dict for storage queries.

    Args:
        block_hash: The block hash or None.

    Returns:
        Scope dictionary.
    """
    return {} if block_hash is None else {"block_hash": block_hash}


def _typed_payload_size(
    expr: Expr, node: Any, contact: Any, block_hash: bytes | None = None
) -> int | None:
    """Payload size if *expr* is a builtin typed value (int, str, float), else None.

    A typed value is a bytes payload tagged by a builtin type symbol. On the
    node that built it, it is one loaded composite. Fetched cell by cell it
    arrives as a pair link whose tail is only the hash of the type symbol.
    Both have the same hash and must be treated the same way, so the test is
    on the hash.
    """
    if expr.base != "link":
        return None
    if is_builtin_composite(expr):
        # Not expr.size(): it is cached, and goes stale when a flat value is
        # later resolved in place.
        if expr.value is not None:
            return len(TAG_BYTE_ENCODINGS[expr.tail.value](expr.value))
        return len(expr.head.value)
    tail_hash = expr._tail_hash
    if tail_hash is None and expr._tail is not None:
        tail_hash = expr._tail.hash()
    if _BUILTIN_TYPE_HASH.get(tail_hash) not in BUILTIN_COMPOSITE_TYPE_NAMES:
        return None
    head = expr._head
    if head is None and expr._head_hash is not None:
        # The payload is part of the cell: it is in the cell's block.
        head = get_expr(node, expr._head_hash, contact=contact, **_scope(block_hash))
        if head is None:
            raise UnresolvedRecordExpr(expr._head_hash)
    if head is None or head.base != "bytes":
        return None
    return len(head.value)


def _flat_size(expr: Expr, node: Any, contact: Any, block_hash: bytes | None = None) -> int:
    """Size of one expr cell independent of how much of its subtree is loaded.

    ``Expr.size()`` adds the sizes of loaded children, so the same expr counts
    differently on a node that holds the whole tree in memory and one that
    fetched it cell by cell. A record that follows hash references is built by
    several nodes and must agree, so a pair link counts as two hashes.

    Args:
        expr: The expression.
        node: The node.
        contact: The contact.
        block_hash: Optional block hash.

    Returns:
        The flat size in bytes.
    """
    typed = _typed_payload_size(expr, node, contact, block_hash)
    if typed is not None:
        return typed
    if expr._tag != "link":
        return expr.size()
    return 64


def _single_cell(expr: Expr, node: Any, contact: Any, block_hash: bytes | None = None) -> Expr:
    """*expr* with its children replaced by their hashes (same hash).

    The record walk slots every cell on its own, so each is stored on its own.
    Storing a loaded tree per cell would write its subtree once per ancestor.

    Args:
        expr: The expression.
        node: The node.
        contact: The contact.
        block_hash: Optional block hash.

    Returns:
        The single cell expression.
    """
    if expr._tag != "link" or _typed_payload_size(expr, node, contact, block_hash) is not None:
        return expr
    cell = Expr("link", head_hash=_get_head_hash(expr), tail_hash=_get_tail_hash(expr))
    return cell if cell.hash() == expr.hash() else expr


def _cell_children(
    sub_expr: Expr, node: Any, contact: Any, follow_refs: bool, block_hash: bytes | None = None
) -> list[tuple[Expr | None, bytes]]:
    """Items to visit below *sub_expr*, in visiting order: (expr or None, hash)."""
    if sub_expr._tag != "link":
        return []
    if follow_refs and _typed_payload_size(sub_expr, node, contact, block_hash) is not None:
        return []  # a value, not a structure: its payload is part of the cell
    items: list[tuple[Expr | None, bytes]] = []
    for child, ref in (
        (sub_expr._head, sub_expr._head_hash),
        (sub_expr._tail, sub_expr._tail_hash),
    ):
        if child is not None:
            child_hash = child.hash()
            # nil is a ZERO32 reference when fetched and a loaded NIL when
            # built; following references treats both as nothing.
            if follow_refs and child_hash == ZERO32:
                continue
            items.append((child, child_hash))
        elif follow_refs and ref is not None and ref != ZERO32:
            items.append((None, ref))
    return items


def storage_walk_size(
    node: Any,
    block: object,
    expr: Expr,
    *,
    exclude: Iterable[bytes] = (),
) -> int | None:
    """Return bytes a block record counts for *expr*.

    Sums ``_flat_size`` over distinct cells (root included), following hash
    references. This is the size a sender is charged. It is the same number
    on a node that holds *expr* whole and on one that fetches it cell by cell.

    Cells whose hash is in *exclude* (and everything below them) are not
    counted nor fetched. Returns None if a referenced cell cannot be fetched.
    """
    contact = getattr(block, "contact", None)
    # A block being built has no id yet; a verified one does, and the cells of
    # its transactions are in its record.
    cell_block = block_scope(block)
    seen: set[bytes] = set(exclude)
    total = 0
    stack: list[tuple[Expr | None, bytes]] = [(expr, expr.hash())]
    try:
        while stack:
            sub_expr, h = stack.pop()
            if h in seen:
                continue
            seen.add(h)
            if sub_expr is None:
                sub_expr = get_expr(node, h, contact=contact, **_scope(cell_block))
                if sub_expr is None:
                    return None
            total += _flat_size(sub_expr, node, contact, cell_block)
            stack.extend(reversed(_cell_children(sub_expr, node, contact, True, cell_block)))
    except UnresolvedRecordExpr:
        return None
    return total


def storage_walk_fee(
    node: Any,
    block: object,
    expr: Expr,
    *,
    exclude: Iterable[bytes] = (),
) -> int | None:
    """``calculate_storage_fee`` on ``storage_walk_size``. None if unresolved."""
    size = storage_walk_size(node, block, expr, exclude=exclude)
    if size is None:
        return None
    return calculate_storage_fee(block, size)


def generate_initial_storage_record(
    node: Any,
    block: object,
    expr: Expr,
    follow_refs: bool = False,
    collected: dict[bytes, Expr] | None = None,
) -> Tuple[StorageRecord, List[bytes], List[bytes], int] | None:
    """
    Walk expr tree, slot new sub-exprs not yet registered in storage account data.
    Returns (StorageRecord, slot_hashes, found_exprs, storage_fee) for a fresh
    expr, or None if already registered.  slot_hashes lists each new sub-expr's
    hash in walk order (depth-first, head before tail). The record commits to
    it as ``slots_root``, the Merkle root over that list, and ``new_count`` is
    its length.  found_exprs contains hashes of sub-exprs already in storage
    data (shared refs).

    By default only sub-exprs loaded into ``expr`` are walked. With
    ``follow_refs`` a hash reference is followed too: it is checked against the
    storage data first and, if new, fetched (local storage, then the block's
    ``contact``) and walked. That is how a block's record covers the tries its
    header only names by root hash. A reference that cannot be fetched raises
    ``UnresolvedRecordExpr``. Sizes then count each cell on its own
    (``_flat_size``), so every node derives the same record. The storage trie
    is followed like any other: a block's record covers the storage trie as of
    the block's start, which registers the previous records. The entries that
    register this record are created in the next block and covered by its
    record.

    ``collected``, if given, receives every slotted expr by hash, as a single
    cell (children replaced by their hashes), so the caller can store them.
    """
    storage_account = block.accounts.get_account(STORAGE_ADDRESS, node)
    storage_data = storage_account.data
    contact = getattr(block, "contact", None)

    root_hash = expr.hash()
    existing = get_from_radix_tree(storage_data, node, root_hash)
    if existing is not None:
        return None  # Already registered — nothing to do

    # Following references looks up thousands of hashes. Read them through a
    # copy so the nodes that only get read do not pile up in the block's
    # storage trie, whose cached nodes are written out with the block.
    lookup = storage_data
    if follow_refs:
        lookup = RadixTree(
            root_hash=storage_data.root_hash,
            contact=storage_data.contact,
            root_block=storage_data.root_block,
        )
        lookup.nodes = dict(storage_data.nodes)

    slot_hashes, found_exprs, total_new_size = walk_record_slots(
        node, expr, lookup, contact=contact, follow_refs=follow_refs, collected=collected
    )

    storage_fee = calculate_storage_fee(block, total_new_size)
    record = StorageRecord(
        creation_block_hash=block.previous_block_hash,
        last_payment_block_hash=block.previous_block_hash,
        last_payment_height=block.height - 1,
        last_payment_winner=ZERO32,
        new_size=total_new_size,
        new_count=len(slot_hashes),
        slots_root=create_merkle_tree(slot_hashes).root,
    )
    return record, slot_hashes, found_exprs, storage_fee


def walk_record_slots(
    node: Any,
    expr: Expr,
    lookup: RadixTree,
    *,
    contact: Any = None,
    follow_refs: bool = False,
    collected: dict[bytes, Expr] | None = None,
    on_cell: Any = None,
) -> Tuple[List[bytes], List[bytes], int]:
    """The walk a block record is built from: ``(slot_hashes, found, size)``.

    Visits the cells below *expr* depth-first, head before tail. A cell whose
    hash is a key in *lookup* (a storage trie) is found, not slotted, and its
    subtree is skipped. Every other cell is slotted, once. *expr* itself is the
    record's key and is not slotted. Cells that are not loaded are fetched
    scoped to *expr*'s hash (the block), from *contact* if given.

    ``collected`` receives each slotted cell by hash and ``on_cell(hash, cell)``
    is called as each is reached, so a caller can store cells while it walks.
    Raises ``UnresolvedRecordExpr`` if a cell cannot be fetched.
    """
    root_hash = expr.hash()
    scope = {"block_hash": root_hash} if follow_refs else {}
    cell_block = root_hash if follow_refs else None
    slot_hashes: list[bytes] = []
    slotted: set[bytes] = set()
    found_exprs: list[bytes] = []
    total_new_size = 0

    # Iterative: a trie walked through its references is far deeper than the
    # interpreter's recursion limit.
    stack = list(reversed(_cell_children(expr, node, contact, follow_refs, cell_block)))
    while stack:
        sub_expr, h = stack.pop()
        if h in slotted or get_from_radix_tree(lookup, node, h) is not None:
            if h not in slotted:
                found_exprs.append(h)
            continue  # Already slotted or shared reference — skip entire subtree
        if sub_expr is None:
            sub_expr = get_expr(node, h, contact=contact, **scope)
            if sub_expr is None:
                raise UnresolvedRecordExpr(h)
        slotted.add(h)
        slot_hashes.append(h)
        total_new_size += (
            _flat_size(sub_expr, node, contact, cell_block) if follow_refs else sub_expr.size()
        )
        if collected is not None or on_cell is not None:
            cell = _single_cell(sub_expr, node, contact, cell_block)
            if collected is not None:
                collected[h] = cell
            if on_cell is not None:
                on_cell(h, cell)
        stack.extend(reversed(_cell_children(sub_expr, node, contact, follow_refs, cell_block)))
    return slot_hashes, found_exprs, total_new_size


def handle_storage_initial_contract(
    *,
    node: Any,
    block: object,
    transaction: Transaction,
    sender_account: Any,
    storage_account: Any,
    expr_list_id: bytes,
    current_fees: int,
) -> int | None:
    """Charge a STORAGE_CREATE transaction for the exprs it lists.

    Returns the charged fee, or None if the list cannot be resolved or the
    sender cannot afford it. No record is written: the listed exprs arrive with
    the transaction tree, and the block record covers them. Resolving the list
    here, instead of leaving it to the record walk, fails the transaction when
    an item cannot be fetched.
    """
    try:
        if transaction is not None:
            tx_exprs, missed = resolve_inner_exprs(
                node, transaction.expr(), contact=block.contact, block_hash=block_scope(block)
            )
            if missed:
                return None
            data_head = transaction.data._head
            if data_head is None or data_head._tag != "link":
                return None
            list_expr = data_head._head
            if list_expr is None or list_expr._tag != "link":
                return None
            list_items, missed = resolve_list_exprs(
                node, list_expr, contact=block.contact, block_hash=block_scope(block)
            )
            if missed:
                return None
        else:
            list_expr = get_expr_list(
                node, expr_list_id, contact=block.contact, **_scope(block_scope(block))
            )
            if list_expr is None:
                return None
            list_items, _ = resolve_list_exprs(
                node, list_expr, contact=block.contact, block_hash=block_scope(block)
            )

        total_bytes = 0
        for item in list_items:
            item_size = storage_walk_size(node, block, item)
            if item_size is None:
                return None
            total_bytes += item_size

        storage_cost = calculate_storage_fee(block, total_bytes)
        if sender_account.balance < current_fees + storage_cost:
            return None

        storage_account.balance += storage_cost
        sender_account.balance -= storage_cost
        return storage_cost
    except Exception:
        return None
