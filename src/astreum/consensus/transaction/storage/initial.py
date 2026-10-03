from __future__ import annotations

from typing import Any, List, Optional, Tuple

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
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot

class UnresolvedRecordExpr(Exception):
    """An expr the record walk had to follow could not be fetched."""

    def __init__(self, expr_id: bytes):
        super().__init__(f"unable to resolve expr {expr_id.hex()} for storage record")
        self.expr_id = expr_id


def _typed_payload_size(expr: Expr, node: Any, contact: Any) -> int | None:
    """Payload size if *expr* is a builtin typed value (int, str, float), else None.

    A typed value is a bytes payload tagged by a builtin type symbol. On the
    node that built it, it is one loaded composite; fetched cell by cell it
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
        head = get_expr(node, expr._head_hash, contact=contact)
        if head is None:
            raise UnresolvedRecordExpr(expr._head_hash)
    if head is None or head.base != "bytes":
        return None
    return len(head.value)


def _flat_size(expr: Expr, node: Any, contact: Any) -> int:
    """Size of one expr cell independent of how much of its subtree is loaded.

    ``Expr.size()`` adds the sizes of loaded children, so the same expr counts
    differently on a node that holds the whole tree in memory and one that
    fetched it cell by cell. A record that follows hash references is built by
    several nodes and must agree, so a pair link counts as two hashes.
    """
    typed = _typed_payload_size(expr, node, contact)
    if typed is not None:
        return typed
    if expr._tag != "link":
        return expr.size()
    return 64


def _single_cell(expr: Expr, node: Any, contact: Any) -> Expr:
    """*expr* with its children replaced by their hashes (same hash).

    The record walk slots every cell on its own, so each is stored on its own;
    storing a loaded tree per cell would write its subtree once per ancestor.
    """
    if expr._tag != "link" or _typed_payload_size(expr, node, contact) is not None:
        return expr
    cell = Expr("link", head_hash=_get_head_hash(expr), tail_hash=_get_tail_hash(expr))
    return cell if cell.hash() == expr.hash() else expr


def generate_initial_storage_record(
    node: Any,
    block: object,
    expr: Expr,
    temp_exprs: dict[bytes, Expr] | None = None,
    mint: bool = False,
    follow_refs: bool = False,
    collected: dict[bytes, Expr] | None = None,
) -> Tuple[StorageRecord, dict[bytes, StorageSlot], List[bytes], int] | None:
    """
    Walk expr tree, slot new sub-exprs not yet registered in storage account data.
    Returns (StorageRecord, slot_map, found_exprs, storage_fee) for a fresh
    expr, or None if already registered.  slot_map maps each new sub-expr's
    hash to its StorageSlot.  found_exprs contains hashes of sub-exprs already
    in storage data (shared refs).

    By default only sub-exprs loaded into ``expr`` are walked. With
    ``follow_refs`` a hash reference is followed too: it is checked against the
    storage data first and, if new, fetched (local storage, then the block's
    ``contact``) and walked. That is how a block's record covers the tries its
    header only names by root hash. A reference that cannot be fetched raises
    ``UnresolvedRecordExpr``. Sizes then count each cell on its own
    (``_flat_size``), so every node derives the same record. The storage trie
    itself (its root as of the block's start) is not followed.

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
    not_followed: set[bytes] = set()
    if follow_refs:
        # The storage trie is the registry of what is stored. Slotting its
        # nodes would register each of them in the trie, and those entries
        # would be new nodes to slot in the next record, a dozen times larger
        # every block. A record does not cover the trie it is registered in.
        if storage_data.root_hash:
            not_followed.add(storage_data.root_hash)
        lookup = RadixTree(root_hash=storage_data.root_hash, contact=storage_data.contact)
        lookup.nodes = dict(storage_data.nodes)

    storage_id = root_hash
    slot_map: dict[bytes, StorageSlot] = {}
    found_exprs: list[bytes] = []
    total_new_size = 0

    def _children(sub_expr: Expr, is_root: bool = False) -> list[tuple[Expr | None, bytes]]:
        """Items to visit below sub_expr, in visiting order: (expr or None, hash)."""
        if sub_expr._tag != "link":
            return []
        if follow_refs and _typed_payload_size(sub_expr, node, contact) is not None:
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
        if not is_root and sub_expr._head_hash is not None and sub_expr._head is None:
            ptr = sub_expr._head_hash
            if get_from_radix_tree(lookup, node, ptr) is None and temp_exprs is not None:
                target = temp_exprs.get(ptr)
                if target is not None and target._tag == "link":
                    for child in (target._head, target._tail):
                        if child is not None:
                            items.append((child, child.hash()))
        return items

    # Depth-first, head before tail. Iterative: a trie walked through its
    # references is far deeper than the interpreter's recursion limit.
    stack = list(reversed(_children(expr, is_root=True)))
    while stack:
        sub_expr, h = stack.pop()
        if h in not_followed:
            continue
        if h in slot_map or get_from_radix_tree(lookup, node, h) is not None:
            if h not in slot_map:
                found_exprs.append(h)
            continue  # Already slotted or shared reference — skip entire subtree
        if sub_expr is None:
            sub_expr = get_expr(node, h, contact=contact)
            if sub_expr is None:
                raise UnresolvedRecordExpr(h)
        slot_map[h] = StorageSlot(
            storage_id=storage_id,
            sequence=len(slot_map),
        )
        total_new_size += (
            _flat_size(sub_expr, node, contact) if follow_refs else sub_expr.size()
        )
        if collected is not None:
            collected[h] = _single_cell(sub_expr, node, contact)
        stack.extend(reversed(_children(sub_expr)))

    storage_fee = calculate_storage_fee(block, total_new_size)
    record = StorageRecord(
        creation_block_hash=block.previous_block_hash,
        last_payment_block_hash=block.previous_block_hash,
        last_payment_height=block.height - 1,
        last_payment_winner=ZERO32,
        new_size=total_new_size,
        new_count=len(slot_map),
        mint=mint,
    )
    return record, slot_map, found_exprs, storage_fee


def build_storage_contract_record(
    *,
    creation_previous_block_hash: bytes,
    creation_height: int,
    new_size: int,
    new_count: int,
) -> tuple[bytes, list[Expr]]:
    record = StorageRecord(
        creation_block_hash=creation_previous_block_hash,
        last_payment_block_hash=creation_previous_block_hash,
        last_payment_height=creation_height - 1,
        last_payment_winner=ZERO32,
        new_size=new_size,
        new_count=new_count,
    )
    return record.expr().hash(), [record.expr()]


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
    """Handle a storage-initial contract transaction and return charged storage fee."""
    try:
        existing_record = get_from_radix_tree(storage_account.data, node, expr_list_id)
        if existing_record is not None:
            return None

        if transaction is not None:
            tx_exprs, missed = resolve_inner_exprs(node, transaction.expr(), contact=block.contact)
            if missed:
                return None
            data_head = transaction.data._head
            if data_head is None or data_head._tag != "link":
                return None
            list_expr = data_head._head
            if list_expr is None or list_expr._tag != "link":
                return None
            list_items, missed = resolve_list_exprs(node, list_expr, contact=block.contact)
            if missed:
                return None
        else:
            list_expr = get_expr_list(node, expr_list_id, contact=block.contact)
            if list_expr is None:
                return None
            list_items, _ = resolve_list_exprs(node, list_expr, contact=block.contact)
        total_bytes = sum(item.size() for item in list_items)
        number_of_exprs = len(list_items)

        storage_cost = calculate_storage_fee(block, total_bytes)
        if sender_account.balance < current_fees + storage_cost:
            return None

        record_value, record_exprs = build_storage_contract_record(
            creation_previous_block_hash=block.previous_block_hash,
            creation_height=block.height,
            new_size=total_bytes,
            new_count=number_of_exprs,
        )

        put_in_radix_tree(storage_account.data, node, expr_list_id, record_value)
        storage_account.data_hash = storage_account.data.root_hash
        storage_account.balance += storage_cost
        sender_account.balance -= storage_cost

        block.pending_exprs.extend(record_exprs)
        return storage_cost
    except Exception:
        return None
