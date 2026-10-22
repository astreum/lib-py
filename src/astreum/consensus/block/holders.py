"""Which peer holds a recent block's uncommitted exprs.

A validator that announces its newest block (``BLOCK`` message) is the only
node that has the block's new exprs until a later block commits them. Nodes
record that peer here and pass it as ``contact`` when they read the block.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from astreum.communication.models.relay_endpoint import RelayEndpoint

MAX_BLOCK_HOLDERS = 16

def init_block_holders(node: Any) -> None:
    """Initialize block holders storage on the node.

    Args:
        node: The node to initialize.

    Returns: None
    """
    node.block_holders = OrderedDict()
    node.block_holders_lock = threading.Lock()


def block_holder(node: Any, block_hash: Optional[bytes]) -> Optional["RelayEndpoint"]:
    """Return the ``RelayEndpoint`` holding *block_hash*, or None.

    Args:
        node: The node to query.
        block_hash: The block hash to look up.

    Returns:
        The endpoint if found, else None.
    """
    holders = getattr(node, "block_holders", None)
    if holders is None or not block_hash:
        return None
    with node.block_holders_lock:
        return holders.get(block_hash)


def has_block_holder(node: Any, block_hash: bytes) -> bool:
    """Check if a block holder exists for the given hash.

    Args:
        node: The node to query.
        block_hash: The block hash to check.

    Returns:
        True if a holder exists, False otherwise.
    """
    return block_holder(node, block_hash) is not None


def record_block_holder(node: Any, block_hash: bytes, contact: "RelayEndpoint") -> bool:
    """Remember *contact* as the holder of *block_hash*.

    Keeps the first holder seen. Returns False if one was already recorded.
    Oldest entries are dropped beyond ``MAX_BLOCK_HOLDERS``.

    Args:
        node: The node to record on.
        block_hash: The block hash.
        contact: The endpoint of the holder.

    Returns:
        True if recorded, False if already exists.
    """
    with node.block_holders_lock:
        if block_hash in node.block_holders:
            return False
        node.block_holders[block_hash] = contact
        while len(node.block_holders) > MAX_BLOCK_HOLDERS:
            node.block_holders.popitem(last=False)
        return True
