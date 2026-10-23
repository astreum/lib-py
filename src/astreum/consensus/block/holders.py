"""Which peer holds a recent block's uncommitted exprs.

A validator that announces its newest block (``BLOCK`` message) is the only
node that has the block's new exprs until a later block commits them. Nodes
record that peer here and pass it as ``contact`` when they read the block.
A node with a validation key takes holders from ``BLOCK`` messages. A node
without one takes holders from pings.
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


def get_ping_holder(
    node: Any, block_hash: Optional[bytes], block: Any = None
) -> Optional["RelayEndpoint"]:
    """Return the holder of a head block for a ping, or None.

    The result is the first of these that exists:

    1. The recorded holder of the block. A validator that adopted the block
       has the contact of its creator here.
    2. The contact of this node, if this node made the block.

    Args:
        node: The node that builds the ping.
        block_hash: The head block hash that the ping carries.
        block: The loaded head block, or None if it is not loaded.

    Returns:
        The endpoint, or None if this node knows no holder. The creator is not
        recorded in ``block_holders``, because its own reads need no contact.
    """
    if not block_hash:
        return None
    holder = block_holder(node, block_hash)
    if holder is not None:
        return holder
    own_key = getattr(node.config, "validation_public_key_bytes", None)
    if block is None or not own_key:
        return None
    if getattr(block, "validator_public_key_bytes", None) != own_key:
        return None
    relay_key = getattr(node.config, "relay_public_key_bytes", None)
    host = getattr(node, "relay_ip_address", None)
    port = getattr(node.config, "port", None)
    if not relay_key or not host or port is None:
        return None
    from astreum.communication.models.relay_endpoint import RelayEndpoint

    return RelayEndpoint(relay_key, (host, int(port)))
