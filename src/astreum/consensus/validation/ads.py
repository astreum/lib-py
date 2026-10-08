"""Advertise the records a validator committed, once the block is adopted.

A validator that creates block B writes the records-table entries for the
records B commits (the transactions' records, and the previous block's record).
Nothing else knows it holds them. Advertising at creation does not work: peers
drop ids that are not in their latest block's storage trie, and B is not their
latest block yet. So the ids wait here until a block has been built on B, then
go out once.
"""

from __future__ import annotations

from collections import deque
from typing import Any, NamedTuple, Optional

from astreum.expression import RESOLUTION_SINGLE, ZERO32

# Blocks whose ads are waiting; the oldest is dropped beyond this.
MAX_PENDING_ADS = 8
# An entry whose block cannot be placed on the chain is dropped once the head
# is this many blocks past it.
MAX_PENDING_AD_AGE = 4


class PendingAd(NamedTuple):
    height: int
    block_hash: bytes
    ids: tuple[bytes, ...]


def init_pending_ads(node: Any) -> None:
    """Initialize pending ads storage on the node.

    Args:
        node: The node to initialize.

    Returns: None
    """
    node.pending_ads = deque(maxlen=MAX_PENDING_ADS)


def queue_ads(node: Any, height: int, block_hash: bytes, ids: list[bytes]) -> None:
    """Hold *ids*, committed by the block at *height*, until it is adopted.

    Args:
        node: The node.
        height: The block height.
        block_hash: The block hash.
        ids: List of expression identifiers.

    Returns: None
    """
    if not ids:
        return
    if getattr(node, "pending_ads", None) is None:
        init_pending_ads(node)
    node.pending_ads.append(PendingAd(height, block_hash, tuple(ids)))


def _block_hash(block: Any) -> Optional[bytes]:
    """Get block hash from a block.

    Args:
        block: The block object.

    Returns:
        The block hash or None.
    """
    expr_id = getattr(block, "expr_id", None)
    if expr_id and expr_id != ZERO32:
        return expr_id
    try:
        from astreum.consensus.block.encoding.expr import get_block_expr

        return get_block_expr(block).hash()
    except Exception:
        return None


def _hash_at_height(node: Any, head: Any, height: int) -> Optional[bytes]:
    """Hash of the block at *height* on the chain ending at *head*.

    None if the chain cannot be followed that far from what this node holds.

    Args:
        node: The node.
        head: The chain head.
        height: The target height.

    Returns:
        The block hash or None.
    """
    from astreum.consensus.block.encoding.decode import get_block_from_storage

    block = head
    while block is not None and block.height > height:
        parent = block.previous_block
        if parent is None:
            try:
                parent = get_block_from_storage(
                    node, block.previous_block_hash, load_bloom=False
                )
            except Exception:
                return None
        block = parent
    if block is None or block.height != height:
        return None
    return _block_hash(block)


def flush_ads(node: Any) -> list[bytes]:
    """Send the ids of every queued block that has been built on. Return them.

    A block is adopted once the head is past its height and the chain to the
    head passes through it. A block the chain skips lost a fork and is dropped
    unsent, as is one that cannot be placed after ``MAX_PENDING_AD_AGE`` blocks.

    Args:
        node: The node.

    Returns:
        List of sent expression identifiers.
    """
    pending = getattr(node, "pending_ads", None)
    if not pending:
        return []
    with node.latest_block_lock:
        head = node.latest_block
    if head is None:
        return []

    ready: list[PendingAd] = []
    keep: list[PendingAd] = []
    for entry in pending:
        if entry.height >= head.height:
            keep.append(entry)
            continue
        on_chain = _hash_at_height(node, head, entry.height)
        if on_chain == entry.block_hash:
            ready.append(entry)
        elif on_chain is not None:
            node.logger.debug(
                "Dropping ads for block #%s %s; it is not on the chain",
                entry.height,
                entry.block_hash.hex(),
            )
        elif head.height - entry.height <= MAX_PENDING_AD_AGE:
            keep.append(entry)
        else:
            node.logger.debug(
                "Dropping ads for block #%s %s; not placed on the chain",
                entry.height,
                entry.block_hash.hex(),
            )
    pending.clear()
    pending.extend(keep)

    from astreum.storage.advertisements import advertise_exprs

    sent: list[bytes] = []
    for entry in ready:
        advertised, warning = advertise_exprs(
            node, [(expr_id, RESOLUTION_SINGLE, None) for expr_id in entry.ids]
        )
        if warning:
            node.logger.warning("Ads for block #%s: %s", entry.height, warning)
        sent.extend(advertised)
    return sent
