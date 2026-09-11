from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from astreum.expression import Expr
from astreum.storage.cold.get import get_expr_from_cold_storage
from astreum.storage.cold.insert import put_expr_in_cold_storage
from astreum.storage.cold.iter import iter_exprs_in_cold_storage

if TYPE_CHECKING:
    from astreum.node import Node


def put_record_in_cold_storage(node: "Node", storage_id: bytes, slot_ids: list[bytes]) -> bool:
    """Write one record's slot list into the records LSM table.

    key = ``storage_id`` (value hash), value = concat of the 32-byte slot
    data ids in sequence order.  Reuses the expr cold-store machinery
    (``put_expr_in_cold_storage``) with the ``records`` table, so
    collate/merge are handled identically.

    Args:
        node: A Node instance providing config and storage access.
        storage_id: The 32-byte key the record is stored under (the
            record value's content hash).
        slot_ids: The 32-byte slot data ids in sequence order.

    Returns:
        True on success, False if cold storage is disabled or an
        I/O error occurs.
    """
    value = b"".join(slot_ids)
    store = Expr("bytes", value=value)
    return put_expr_in_cold_storage(
        node,
        store,
        table="records",
        size_attr="records_level_0_size",
        key=storage_id,
    )


def get_record_from_cold_storage(node: "Node", storage_id: bytes) -> bytes | None:
    """Return the raw concat value blob for a record, or None.

    Reads the record entry from the records table only (no hot cache,
    no network fetch).

    Args:
        node: A Node instance providing ``config`` and ``cold_storage_lock``.
        storage_id: The 32-byte key the record is stored under.

    Returns:
        The concatenated slot-id blob, or ``None`` if the record is
        absent or its data is malformed.
    """
    expr = get_expr_from_cold_storage(node, storage_id, table="records")
    if expr is None:
        return None
    return expr.value


def iter_records_in_cold_storage(node: "Node") -> Iterator[bytes]:
    """Yield record hashes (keys) in the records table, one at a time.

    The lock is held only for each segment read (never across the whole
    scan), so collate/merge — which delete source files — cannot race
    the listing while other cold reads are happening.

    Args:
        node: A Node instance providing ``config`` and ``cold_storage_lock``.

    Yields:
        The 32-byte record hash of each stored record.
    """
    yield from iter_exprs_in_cold_storage(node, table="records")
