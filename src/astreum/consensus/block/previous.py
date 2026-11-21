"""Get the parent of a block."""

from __future__ import annotations

from typing import Any, Optional

from astreum.expression import ZERO32
from astreum.consensus.block.holders import block_holder


def get_previous_block(node: Any, block: Any) -> Optional[Any]:
    """Return the parent of *block*, loading it by hash when it is not linked.

    The function does not set ``block.previous_block``. The caller decides
    whether to keep the result.

    Args:
        node: The node used to read storage.
        block: The block whose parent to get.

    Returns:
        The parent block, or None if *block* is a genesis block or the parent
        cannot be loaded. The parent has no bloom tree loaded.
    """
    previous = block.previous_block
    if previous is not None:
        return previous

    prev_hash = block.previous_block_hash
    if not prev_hash or prev_hash == ZERO32:
        return None

    from astreum.consensus.block.encoding.decode import get_block_from_storage

    contact = block_holder(node, prev_hash) or block.contact
    try:
        return get_block_from_storage(
            node, prev_hash, contact=contact, load_bloom=False
        )
    except Exception as exc:
        node.logger.debug(
            "Unable to load previous block %s: %s", prev_hash.hex(), exc
        )
        return None
