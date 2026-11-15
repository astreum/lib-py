from __future__ import annotations

from typing import Any, Optional

from astreum.expression import ZERO32
from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.block.previous import get_previous_block
from astreum.crypto.bloom_search.search import ERA_SIZE


def find_block_by_height(astreum_node: Any, *,
                         starting_block=None,
                         target_height: int) -> Optional[Block]:
    """Find a block by height using the bloom tree's binary structure.

    The search crosses eras through the era heads. An era head names its
    parent in ``previous_era_hash``, and that parent is the previous era's last
    block, whose bloom tree covers the whole era. So a hop back one era costs
    one parent fetch and one descent, not a walk over every block.

    Args:
        astreum_node: Astreum node.
        starting_block: Block to start search from.
        target_height: Target block height.

    Returns:
        Optional[Block]: The block at ``target_height``, or None when the
        block is not reachable from ``starting_block``.
    """
    if starting_block is None:
        return None
    if target_height > starting_block.height:
        return None

    target_era = target_height // ERA_SIZE
    current = starting_block

    # Hop era by era: current becomes the previous era's last block, which is
    # also its most complete bloom tree.
    while current is not None and current.height // ERA_SIZE > target_era:
        head = _era_head(astreum_node, current)
        if head is None:
            return None
        current = get_previous_block(astreum_node, head)

    if current is None or current.height // ERA_SIZE != target_era:
        return None  # target era not reachable from this starting block

    return _block_in_era(astreum_node, current, target_height)


def _era_head(astreum_node: Any, block: Any) -> Optional[Any]:
    """Return the first block of the era that holds *block*.

    Args:
        astreum_node: Astreum node.
        block: A block in the era.

    Returns:
        The block at offset 0 of the era, or None when it cannot be read.
    """
    if block.height % ERA_SIZE == 0:
        return block

    if block.bloom_hash and block.bloom_hash != ZERO32:
        leaf_hash = _storage_find_leaf(
            astreum_node, block.bloom_hash, 0, block_hash=block.expr_id
        )
        if leaf_hash is not None:
            head = get_block_from_storage(
                astreum_node=astreum_node, block_hash=leaf_hash
            )
            if head is not None and head.height % ERA_SIZE == 0:
                return head

    # No tree, or a tree that does not reach offset 0: walk the parents.
    return _walk_back(
        astreum_node, block, lambda b: b.height % ERA_SIZE == 0
    )


def _block_in_era(
    astreum_node: Any, block: Any, target_height: int
) -> Optional[Any]:
    """Return the block at *target_height* within the era that holds *block*.

    Args:
        astreum_node: Astreum node.
        block: A block in the target era.
        target_height: Height to find.

    Returns:
        The block at the height, or None when it has not been mined.
    """
    if block.height == target_height:
        return block

    target_offset = target_height % ERA_SIZE
    if target_offset > block.height % ERA_SIZE:
        return None  # the target offset is not mined in this era

    if block.bloom_hash and block.bloom_hash != ZERO32:
        leaf_hash = _storage_find_leaf(
            astreum_node, block.bloom_hash, target_offset, block_hash=block.expr_id
        )
        if leaf_hash is not None:
            return get_block_from_storage(
                astreum_node=astreum_node, block_hash=leaf_hash
            )

    # No tree, or no leaf on the path: walk the parents.
    return _walk_back(astreum_node, block, lambda b: b.height <= target_height)


def _walk_back(astreum_node: Any, block: Any, stop) -> Optional[Any]:
    """Walk parents until *stop* holds for the current block.

    Args:
        astreum_node: Astreum node.
        block: Block to start the walk from.
        stop: Predicate that takes a block and returns True to stop.

    Returns:
        The first block that satisfies *stop*, or None when the chain ends.
    """
    current = block
    while current is not None and not stop(current):
        current = get_previous_block(astreum_node, current)
    return current


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
