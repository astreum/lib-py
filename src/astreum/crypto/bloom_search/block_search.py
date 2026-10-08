from __future__ import annotations

from typing import Any, Optional

from astreum.expression import ZERO32
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.crypto.bloom_search.search import ERA_SIZE


def find_block_by_height(astreum_node: Any, *,
                         starting_block=None,
                         target_height: int) -> Optional[Block]:
    """Find a block by height using the bloom tree's binary structure.

    Args:
        astreum_node: Astreum node.
        starting_block: Block to start search from.
        target_height: Target block height.

    Returns:
        Optional[Block]: Description of the return value.
    """
    if starting_block is None:
        return None
    if target_height > starting_block.height:
        return None

    target_era = target_height // ERA_SIZE
    target_offset = target_height % ERA_SIZE

    # Walk backward via previous_block chain until we find a block in the
    # target era.  The first hit is the highest block we have in that era
    # (its bloom_tree is the most complete).
    current = starting_block
    while current is not None and current.height // ERA_SIZE > target_era:
        current = current.previous_block

    if current is None or current.height // ERA_SIZE != target_era:
        return None  # target era not reachable from this starting block

    # Fast path: current IS the target block (e.g. genesis, era head).
    if current.height == target_height:
        return current

    # Check whether the target offset has been mined.
    if target_offset > current.height % ERA_SIZE:
        return None

    # No bloom tree — walk previous_block chain to find the target.
    if not current.bloom_hash or current.bloom_hash == ZERO32:
        target = current
        while target is not None and target.height > target_height:
            if target.previous_block is not None:
                target = target.previous_block
            else:
                prev_hash = getattr(target, "previous_block_hash", None)
                if prev_hash:
                    target = get_block_from_storage(astreum_node=astreum_node, block_hash=prev_hash)
                else:
                    target = None
        return target

    # Binary-descent by offset to locate the leaf's start_hash.
    leaf_hash = _storage_find_leaf(
        astreum_node, current.bloom_hash, target_offset, block_hash=current.expr_id
    )
    if leaf_hash is not None:
        return get_block_from_storage(astreum_node=astreum_node, block_hash=leaf_hash)

    # Leaf found but start_hash deferred — target is the era head itself.
    # Walk backward from current to find it.
    target = current
    while target is not None and target.height > target_height:
        if target.previous_block is not None:
            target = target.previous_block
        else:
            prev_hash = getattr(target, "previous_block_hash", None)
            if prev_hash:
                target = get_block_from_storage(astreum_node=astreum_node, block_hash=prev_hash)
            else:
                target = None
    return target


def _storage_find_leaf(astreum_node: Any, root_hash: bytes,
                       offset: int, block_hash: Optional[bytes] = None) -> Optional[bytes]:
    """Binary-descent by position in the bloom tree.

    Args:
        astreum_node: Astreum node.
        root_hash: Root node hash.
        offset: Target offset in the tree.
        block_hash: Block holding the root.

    Returns:
        Optional[bytes]: Description of the return value.
    """
    from astreum.crypto.bloom_tree.tree import read_bloom_node

    node = read_bloom_node(astreum_node, root_hash, block=block_hash or None, level=0)
    if node is None:
        return None

    lo, hi = 0, ERA_SIZE

    while not node.is_leaf:
        mid = (lo + hi) // 2
        if offset < mid:
            child_hash, child_block = node._left_hash, node._left_block
        else:
            child_hash, child_block = node._right_hash, node._right_block
        if child_hash is None:
            return None  # path doesn't exist — block at this offset not mined
        node = read_bloom_node(
            astreum_node, child_hash, block=child_block, level=node.level + 1
        )
        if node is None:
            return None
        lo, hi = (lo, mid) if offset < mid else (mid, hi)

    return node.start_hash
