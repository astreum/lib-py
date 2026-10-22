from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astreum.crypto.merkle import create_merkle_tree
from astreum.storage.cold.insert import put_expr_in_cold_storage
from astreum.storage.exprs import get_expr
from astreum.expression.helpers import get_expr_builtin_tag
from astreum.storage.radix import RadixTree
from astreum.storage.records.table import put_record_in_cold_storage

if TYPE_CHECKING:
    from astreum.consensus.transaction.storage.model import storage_record_from_expr
    from astreum.node import Node


def parse_record(node: "Node", value: Any) -> "StorageRecord | None":
    """Parse a storage-trie value as a ``StorageRecord``, or None if it is not one.

    The value is the header stored in the storage trie at a record's key (the
    block's expr hash). The layout lives in ``storage_record_from_expr``.

    Args:
        node: A Node instance for decoding expressions.
        value: The expression value from the storage trie.

    Returns:
        The parsed StorageRecord, or None if the value is not a record.
    """
    if value is None or get_expr_builtin_tag(value) != "link":
        return None
    from astreum.consensus.transaction.storage.model import storage_record_from_expr

    try:
        return storage_record_from_expr(node, value)
    except (AttributeError, TypeError, ValueError):
        return None


def parse_record_new_count(node: "Node", value: Any) -> int | None:
    """``new_count`` of the record in a storage-trie value, or None.

    Args:
        node: A Node instance for decoding expressions.
        value: The expression value from the storage trie.

    Returns:
        The new_count field, or None if value is not a valid record.
    """
    record = parse_record(node, value)
    return None if record is None else record.new_count


def fetch_and_store_record(
    node: "Node",
    storage_id: bytes,
    record: "StorageRecord",
    contact=None,
) -> bool:
    """Replay a block's record walk, cold-write its cells and index its slots.

    *storage_id* is the block's expr hash and *record* the record the chain
    holds for it. The walk is the one the validators ran,
    ``walk_record_slots`` with ``follow_refs``, against the storage trie of the
    block itself (the trie its record was built against). Cells are fetched one
    by one, scoped to the block, from *contact* (default: the block's known
    holder), so the record is not bounded by one response page, and each is
    cold-written as it arrives.

    The records-table entry is written only if the replay gives the record's
    ``new_count``, ``new_size`` and ``slots_root``. A missing, extra, altered or
    reordered cell gives a different root and the record is rejected. The slot
    list is never re-derived from the current storage trie.

    Args:
        node: A Node instance for fetching and storing data.
        storage_id: The 32-byte block hash (record key).
        record: The StorageRecord describing the block's storage.
        contact: Optional ``RelayEndpoint`` for fetches.

    Returns:
        True on success, False if a cell could not be fetched, the replay
        does not match the record, or the table write failed.
    """
    from astreum.consensus.block.encoding.decode import get_block_from_storage
    from astreum.consensus.block.holders import block_holder
    from astreum.consensus.constants import STORAGE_ADDRESS
    from astreum.consensus.transaction.storage.initial import (
        UnresolvedRecordExpr,
        walk_record_slots,
    )

    if contact is None:
        contact = block_holder(node, storage_id)
    try:
        block = get_block_from_storage(node, storage_id, contact=contact, load_bloom=False)
        storage_account = block.accounts.get_account(STORAGE_ADDRESS, node)
        if storage_account is None:
            return False
        root = get_expr(node, storage_id, contact=contact, block_hash=storage_id)
        if root is None:
            return False
        lookup = RadixTree(
            root_hash=storage_account.data.root_hash,
            contact=contact,
            root_block=storage_account.data.root_block,
        )
        slot_hashes, _, size = walk_record_slots(
            node,
            root,
            lookup,
            contact=contact,
            follow_refs=True,
            on_cell=lambda _h, cell: put_expr_in_cold_storage(node, cell),
        )
    except (UnresolvedRecordExpr, ValueError):
        return False

    if len(slot_hashes) != record.new_count or size != record.new_size:
        return False
    if create_merkle_tree(slot_hashes).root != record.slots_root:
        return False
    put_expr_in_cold_storage(node, root)
    stored = put_record_in_cold_storage(node, storage_id, slot_hashes)
    if stored:
        _announce_record(node, storage_id)
    return stored


def _announce_record(node: "Node", storage_id: bytes) -> None:
    """Announce this node as a provider of a record it has just stored.

    The record is already in cold storage, so a reply never names this node
    before it holds the data. The index ranks providers by distance: this node
    goes first if it is nearer than the listed providers. An error is logged
    and does not change the result of the store.

    Args:
        node: A Node instance that stored the record.
        storage_id: The 32-byte block hash (record key).

    Returns:
        None.
    """
    from astreum.expression import RESOLUTION_SINGLE
    from astreum.storage.exprs.network import put_exprs_in_network

    try:
        put_exprs_in_network(node, [(storage_id, RESOLUTION_SINGLE)])
    except Exception as exc:
        node.logger.debug("Record announce failed for %s: %s", storage_id.hex(), exc)
