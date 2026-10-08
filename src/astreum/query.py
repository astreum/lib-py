"""User-facing query API for the Astreum chain.

Usage::

    from astreum import get_block, find_transactions

    block = get_block(node, height=5000)
    txs = find_transactions(node, sender=addr, limit=10)
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.consensus.models.block import Block
    from astreum.consensus.transaction.model import Transaction


def get_block(node, *, height: int) -> Optional["Block"]:
    """Fetch a block by its chain height.

    Returns ``None`` if the block hasn't been mined yet or is not reachable
    from the node's current tip.

    Args:
        node: Node instance to query.
        height: Chain height of the target block.
    """
    from astreum.crypto.bloom_search.block_search import find_block_by_height

    return find_block_by_height(
        node,
        starting_block=node.latest_block,
        target_height=height,
    )


def find_transactions(
    node,
    *,
    tx_hash: bytes = b"\x00" * 32,
    sender: bytes = b"\x00" * 32,
    receiver: bytes = b"\x00" * 32,
    key: bytes = b"\x00" * 32,
    start_height: Optional[int] = None,
    end_height: int = 0,
    limit: int = 1,
) -> List["Transaction"]:
    """Search for transactions matching the given filters.

    All filter parameters are optional — pass ``ZERO32`` (all zeros) or
    the default to leave a field unconstrained.

    When multiple filters are set, only transactions matching **all**
    of them are returned (AND semantics).

    ``start_height`` — search backward from this height (default:
    the node's latest block).

    ``end_height`` — stop searching when blocks drop below this
    height (default: ``0`` — search the entire chain).

    Set ``limit=0`` for no limit.

    Args:
        node: Node instance to query.
        tx_hash: Transaction hash filter.
        sender: Sender address filter.
        receiver: Receiver address filter.
        key: Storage key filter.
        start_height: Height to start searching backward from.
        end_height: Height to stop searching at.
        limit: Maximum number of transactions to return.
    """
    from astreum.crypto.bloom_search import bloom_search_tx
    from astreum.crypto.bloom_search.block_search import find_block_by_height

    if start_height is None:
        start_block = node.latest_block
    else:
        start_block = find_block_by_height(
            node,
            starting_block=node.latest_block,
            target_height=start_height,
        )
        if start_block is None:
            return []

    return bloom_search_tx(
        node,
        tx_hash=tx_hash,
        sender=sender,
        receiver=receiver,
        key=key,
        starting_block=start_block,
        end_block_height=end_height,
        limit=limit,
    )
