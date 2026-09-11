from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astreum.expression import get_expr_tag, get_expr_value, resolve_list_exprs
from astreum.storage.cold.insert import put_expr_in_cold_storage
from astreum.storage.exprs.local import get_expr_from_local_storage
from astreum.storage.radix import RadixTree, get_from_radix_tree
from astreum.storage.records.table import put_record_in_cold_storage

if TYPE_CHECKING:
    from astreum.node import Node


def parse_slot(node: "Node", value: Any) -> tuple[bytes, int] | None:
    """Classify a storage-trie value as a ``StorageSlot``.

    A slot is ``Link(head_hash=storage_id, tail=Int(sequence))``.  Anything
    else (a record header, malformed data, an unresolved hash) is not a slot.

    Args:
        node: A Node instance providing config and storage access.
        value: The storage-trie value to classify.

    Returns:
        A ``(storage_id, sequence)`` tuple, or ``None`` if the value
        is not a slot.
    """
    if value is None or getattr(value, "_tag", None) != "link":
        return None
    if value._head_hash is None:
        return None
    tail = value._tail
    if tail is None and value._tail_hash is not None:
        tail = get_expr_from_local_storage(node, value._tail_hash)
    if tail is None:
        return None
    try:
        if get_expr_tag(tail, node) != "int":
            return None
        return value._head_hash, get_expr_value(tail, node)
    except (AttributeError, TypeError, ValueError):
        return None


def parse_record_new_count(node: "Node", value: Any) -> int | None:
    """Extract ``new_count`` from a ``StorageRecord`` header expr.

    The header is the value stored in the storage trie at the record's key
    (the block's expr hash).  Mirrors ``StorageRecord.from_storage`` layout:
    6 or 7 elements with ``new_count`` at index 4 or 5.

    Args:
        node: A Node instance providing config and storage access.
        value: The storage-trie value holding the record header.

    Returns:
        The record's ``new_count``, or ``None`` if the value is not a
        well-formed record header.
    """
    if value is None or getattr(value, "_tag", None) != "link":
        return None
    nodes, missed = resolve_list_exprs(node, value)
    if missed or len(nodes) not in (6, 7):
        return None
    mint_idx = 3
    count_idx = 5 if len(nodes) == 7 else 4
    try:
        if get_expr_tag(nodes[mint_idx], node) != "int":
            return None
        if get_expr_tag(nodes[count_idx], node) != "int":
            return None
        return get_expr_value(nodes[count_idx], node)
    except (AttributeError, TypeError, ValueError):
        return None


def collect_record_slots(
    node: "Node",
    tree: RadixTree,
    block_expr: Any,
    storage_id: bytes,
    new_count: int,
) -> list[bytes]:
    """Walk a block expr, deriving its record's slot list.

    For each descendant link-node hash the storage trie is consulted:

    * a ``StorageSlot`` whose ``storage_id`` matches *storage_id* fills
      its position (``sequence``) in a ``new_count * 32`` zero-filled concat
      and its subtree is still walked (descendants are also this record's
      slots);
    * a slot or record belonging to a different record short-circuits the
      subtree;
    * unregistered exprs are walked through.

    Returns the concat as 32-byte slot ids, zeros marking released or
    unavailable positions (the claim worker treats ``ZERO32`` as unclaimable).

    Args:
        node: A Node instance providing config and storage access.
        tree: The storage-account radix trie to consult.
        block_expr: The block expr whose descendants are walked.
        storage_id: The 32-byte key identifying this record.
        new_count: The record's slot count; sizes the returned concat.

    Returns:
        A list of ``new_count`` 32-byte slot ids in sequence order; zero
        entries mark released or unavailable positions.
    """
    concat = bytearray(new_count * 32)
    seen: set[bytes] = set()
    stack: list[Any] = [block_expr]

    while stack:
        expr = stack.pop()
        if expr is None or getattr(expr, "_tag", None) != "link":
            continue
        h = expr.hash()
        if h in seen:
            continue
        seen.add(h)

        if h != storage_id:
            value = get_from_radix_tree(tree, node, h)
            if value is not None:
                slot = parse_slot(node, value)
                if slot is None:
                    continue
                slot_storage_id, sequence = slot
                if slot_storage_id != storage_id:
                    continue
                if 0 <= sequence < new_count:
                    offset = sequence * 32
                    concat[offset : offset + 32] = h

        if expr._head is not None:
            stack.append(expr._head)
        if expr._tail is not None:
            stack.append(expr._tail)

    return [bytes(concat[i : i + 32]) for i in range(0, len(concat), 32)]


def fetch_and_store_record(
    node: "Node",
    storage_id: bytes,
    tree: RadixTree,
    new_count: int,
) -> bool:
    """Fetch a record's data expr (lazily, one sub-expr at a time), write every
    expr to cold storage, and write its records-table entry.

    Trie semantics mirror :func:`collect_record_slots` exactly: slot for this
    record fills its position and the subtree is still walked; slot for
    another record or a non-slot trie value skips the subtree; no trie value
    walks through.  The root is fetched with ``RESOLUTION_RECORD`` so the
    provider returns the record's own slot exprs alongside it; children are
    then resolved from local storage only, and each resolved expr is
    cold-written immediately (which also sidesteps hot-storage LRU eviction
    between fetch and write).  The records-table entry is written only once
    the walk completes, so a failed walk leaves the records table untouched.

    Args:
        node: A Node instance providing config and storage access.
        storage_id: The 32-byte key identifying this record.
        tree: The storage-account radix trie to consult.
        new_count: The record's slot count.

    Returns:
        True on success, False if the record data could not be fetched
        or the records-table write failed.
    """
    from astreum.storage.exprs.network import get_expr_from_network
    from astreum.expression import RESOLUTION_RECORD

    root = get_expr_from_local_storage(node, storage_id)
    if root is None:
        root = get_expr_from_network(node, storage_id, RESOLUTION_RECORD)
    if root is None:
        return False
    put_expr_in_cold_storage(node, root)

    concat = bytearray(new_count * 32)
    seen: set[bytes] = set()
    stack: list[Any] = [root]

    while stack:
        expr = stack.pop()
        if expr is None or getattr(expr, "_tag", None) != "link":
            continue
        h = expr.hash()
        if h in seen:
            continue
        seen.add(h)

        if h != storage_id:
            value = get_from_radix_tree(tree, node, h)
            if value is not None:
                slot = parse_slot(node, value)
                if slot is None:
                    continue
                slot_storage_id, sequence = slot
                if slot_storage_id != storage_id:
                    continue
                if 0 <= sequence < new_count:
                    concat[sequence * 32 : sequence * 32 + 32] = h

        put_expr_in_cold_storage(node, expr)

        # Children resolve from local storage only: the record-aware response
        # already delivered this record's slots.  References into other records
        # are left unresolved as hash references.
        if expr._head is None and expr._head_hash is not None:
            resolved = get_expr_from_local_storage(node, expr._head_hash)
            if resolved is not None:
                expr._head = resolved
                expr._head_hash = None
        if expr._tail is None and expr._tail_hash is not None:
            resolved = get_expr_from_local_storage(node, expr._tail_hash)
            if resolved is not None:
                expr._tail = resolved
                expr._tail_hash = None
        if expr._head is not None:
            stack.append(expr._head)
        if expr._tail is not None:
            stack.append(expr._tail)

    slot_ids = [bytes(concat[i : i + 32]) for i in range(0, len(concat), 32)]
    return put_record_in_cold_storage(node, storage_id, slot_ids)
