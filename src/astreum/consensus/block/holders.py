"""Which peer holds a recent block's uncommitted exprs.

A validator that announces its newest block (``BLOCK`` message) is the only
node that has the block's new exprs until a later block commits them. Nodes
record that peer here and pass it as ``contact`` when they read the block.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

MAX_BLOCK_HOLDERS = 16

Contact = Tuple[bytes, str, int]


def init_block_holders(node: Any) -> None:
    node.block_holders = OrderedDict()
    node.block_holders_lock = threading.Lock()


def block_holder(node: Any, block_hash: Optional[bytes]) -> Optional[Contact]:
    """Return the ``(relay_key, address, port)`` holding *block_hash*, or None."""
    holders = getattr(node, "block_holders", None)
    if holders is None or not block_hash:
        return None
    with node.block_holders_lock:
        return holders.get(block_hash)


def has_block_holder(node: Any, block_hash: bytes) -> bool:
    return block_holder(node, block_hash) is not None


def record_block_holder(node: Any, block_hash: bytes, contact: Contact) -> bool:
    """Remember *contact* as the holder of *block_hash*.

    Keeps the first holder seen. Returns False if one was already recorded.
    Oldest entries are dropped beyond ``MAX_BLOCK_HOLDERS``.
    """
    with node.block_holders_lock:
        if block_hash in node.block_holders:
            return False
        node.block_holders[block_hash] = contact
        while len(node.block_holders) > MAX_BLOCK_HOLDERS:
            node.block_holders.popitem(last=False)
        return True
