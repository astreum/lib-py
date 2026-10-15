from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from astreum.consensus.constants import PROVIDERS_PER_RECORD
from astreum.expression import Expr, NIL, resolve_list_exprs, link, int_, bytes_, exprs_to_linked_expr
from astreum.expression import ZERO32, get_expr_tag, get_expr_builtin_tag, get_expr_value
from astreum.storage.exprs import get_expr


@dataclass(frozen=True)
class StorageLane:
    """One payment lane of a storage record.

    Attributes:
        block_hash: Hash of the block before the last payment on this lane.
        height: Height of the last payment on this lane.
        winner: Address that won the last payment on this lane.
    """

    block_hash: bytes = b""
    height: int = 0
    winner: bytes = ZERO32


def _empty_lanes() -> tuple[StorageLane, ...]:
    return tuple(StorageLane() for _ in range(PROVIDERS_PER_RECORD))


@dataclass
class StorageRecord:
    """A storage record: one shared core and ``PROVIDERS_PER_RECORD`` lanes.

    Conversion to and from expressions is done by the ``storage_record_*``
    functions in this module.

    Attributes:
        creation_block_hash: Hash of the block that created the record.
        lanes: The payment lanes.
        new_size: Total size of the record cells in bytes.
        new_count: Number of slots in the record.
        slots_root: Merkle root of the slot ids.
    """

    creation_block_hash: bytes = b""
    lanes: tuple[StorageLane, ...] = field(default_factory=_empty_lanes)
    new_size: int = 0
    new_count: int = 0
    slots_root: bytes = ZERO32
    _expr: Optional[Expr] = field(default=None, repr=False, compare=False)


def storage_lane_to_expr(lane: StorageLane) -> Expr:
    """Convert a lane to an expression.

    Layout: ``(block_hash height winner)``.

    Args:
        lane: The lane.

    Returns:
        The expression representation.
    """
    tail: Expr = link(bytes_(lane.winner), NIL)
    tail = link(int_(lane.height), tail)
    return link(Expr("link", head_hash=lane.block_hash), tail)


def storage_lane_from_expr(
    node: Any, expr: Optional[Expr], contact=None, block_hash=None
) -> StorageLane | None:
    """Parse a lane expression.

    Args:
        node: The node used to resolve cells.
        expr: The lane expression.
        contact: Optional contact for fetches.
        block_hash: Optional block whose record holds the cells.

    Returns:
        The lane, or None if the expression is not a lane.
    """
    if expr is None or get_expr_builtin_tag(expr) != "link":
        return None
    items, missed = resolve_list_exprs(node, expr, contact=contact, block_hash=block_hash)
    if missed or len(items) != 3:
        return None
    hash_item, height_item, winner_item = items
    if get_expr_tag(hash_item, node, contact, block_hash) != "link":
        return None
    if get_expr_tag(height_item, node, contact, block_hash) != "int":
        return None
    if get_expr_tag(winner_item, node, contact, block_hash) != "bytes":
        return None
    return StorageLane(
        block_hash=_extract_hash(hash_item),
        height=get_expr_value(height_item, node, contact, block_hash),
        winner=get_expr_value(winner_item, node, contact, block_hash),
    )


def storage_record_to_expr(record: StorageRecord) -> Expr:
    """Get the record expression. The result is cached on the record.

    Layout: ``(lanes creation_block_hash slots_root new_count new_size)``,
    where ``lanes`` is ``(lane0 lane1)``.

    Args:
        record: The record.

    Returns:
        The expression representation.
    """
    if record._expr is not None:
        return record._expr
    lanes = exprs_to_linked_expr([storage_lane_to_expr(lane) for lane in record.lanes])
    tail: Expr = link(int_(record.new_size), NIL)
    tail = link(int_(record.new_count), tail)
    # A bytes value, not a hash link: the root names no stored expr, and
    # the record walk follows every hash link.
    tail = link(bytes_(record.slots_root), tail)
    tail = link(Expr("link", head_hash=record.creation_block_hash), tail)
    tail = link(lanes, tail)
    record._expr = tail
    return tail


def storage_record_cells(record: StorageRecord) -> list[Expr]:
    """The record's own exprs, to store with the block that writes it.

    Only what the record holds: the hashes it names (blocks) are left
    unresolved, so this never fetches and never pulls a block's tree in.
    Equal cells (such as two equal lanes) appear once.

    Args:
        record: The record.

    Returns:
        List of expressions.
    """
    out: list[Expr] = []
    seen: set[bytes] = set()
    stack = [storage_record_to_expr(record)]
    while stack:
        e = stack.pop()
        h = e.hash()
        if h in seen or h == ZERO32:
            continue
        seen.add(h)
        out.append(e)
        if get_expr_builtin_tag(e) == "link":
            for child in (e.tail, e.head):
                if child is not None:
                    stack.append(child)
    return out


def _extract_hash(n: Expr) -> bytes:
    """Extract the stored hash from a node, whether resolved or not.

    Args:
        n: The expression node.

    Returns:
        The hash bytes or ZERO32.
    """
    if get_expr_builtin_tag(n) == "link":
        if n.head_hash is not None:
            return n.head_hash
        if n.head is not None:
            return n.head.hash()
    return ZERO32


def storage_record_from_storage(
    node: Any, expr_id: bytes, contact=None, block_hash=None
) -> StorageRecord | None:
    """Load a record by expression id.

    Args:
        node: The node used to resolve cells.
        expr_id: Hash of the record expression.
        contact: Optional contact for fetches.
        block_hash: The block whose record holds the record's cells.

    Returns:
        The record, or None if it is missing or invalid.
    """
    scope = {} if block_hash is None else {"block_hash": block_hash}
    header = get_expr(node, expr_id, contact=contact, **scope)
    return storage_record_from_expr(node, header, contact=contact, block_hash=block_hash)


def storage_record_from_expr(
    node: Any, header: Optional[Expr], contact=None, block_hash=None
) -> StorageRecord | None:
    """Parse a record header already in hand (the layout lives here).

    Args:
        node: The node used to resolve cells.
        header: The record expression.
        contact: Optional contact for fetches.
        block_hash: Optional block whose record holds the cells.

    Returns:
        The record, or None if the expression is not a record.
    """
    if header is None or get_expr_builtin_tag(header) != "link":
        return None
    nodes, missed = resolve_list_exprs(node, header, contact=contact, block_hash=block_hash)
    if missed or len(nodes) != 5:
        return None
    lanes_node, creation_node, root_node, count_node, size_node = nodes
    if get_expr_tag(lanes_node, node, contact, block_hash) != "link":
        return None
    if get_expr_tag(creation_node, node, contact, block_hash) != "link":
        return None
    if get_expr_tag(root_node, node, contact, block_hash) != "bytes":
        return None
    if not all(get_expr_tag(n, node, contact, block_hash) == "int" for n in (count_node, size_node)):
        return None
    lane_nodes, missed = resolve_list_exprs(node, lanes_node, contact=contact, block_hash=block_hash)
    if missed or len(lane_nodes) != PROVIDERS_PER_RECORD:
        return None
    lanes = []
    for lane_node in lane_nodes:
        lane = storage_lane_from_expr(node, lane_node, contact=contact, block_hash=block_hash)
        if lane is None:
            return None
        lanes.append(lane)
    record = StorageRecord(
        creation_block_hash=_extract_hash(creation_node),
        lanes=tuple(lanes),
        slots_root=get_expr_value(root_node, node, contact, block_hash),
        new_count=get_expr_value(count_node, node, contact, block_hash),
        new_size=get_expr_value(size_node, node, contact, block_hash),
    )
    record._expr = header
    return record
