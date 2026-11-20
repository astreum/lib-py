from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum import Node


def discover_latest_block(node: "Node") -> None:
    """Load the trusted seed's head block until it is in memory.

    A node with ``default_seed`` set follows that seed as its head provider
    and skips the verify worker, so nothing else turns the head hash a ping
    carries into a head block. This worker does that: it asks the recorded
    holder of the announced head hash for the block and adopts it. It runs in
    its own thread so the incoming message processor stays free to receive
    the storage responses the fetch waits for.

    Args:
        node (Node): The node that follows its default seed.

    Returns: None. Runs until ``communication_stop_event`` is set.
    """
    from astreum.consensus.block.encoding.decode import get_block_from_storage
    from astreum.consensus.block.holders import block_holder

    stop = node.communication_stop_event
    node.logger.info("Latest block discovery started")
    while not stop.is_set():
        with node.latest_block_lock:
            head_hash = node.latest_block_hash
            have_head = node.latest_block is not None
        if have_head or head_hash is None:
            stop.wait(0.5)
            continue

        holder = block_holder(node, head_hash)
        if holder is None:
            stop.wait(0.5)
            continue

        try:
            block = get_block_from_storage(
                astreum_node=node,
                block_hash=head_hash,
                contact=holder,
            )
        except Exception as exc:
            node.logger.debug(
                "Unable to load head block %s from its holder: %s",
                head_hash.hex(),
                exc,
            )
            stop.wait(0.5)
            continue

        if block is None:
            stop.wait(0.5)
            continue

        adopted = False
        with node.latest_block_lock:
            if node.latest_block_hash == head_hash:
                node.latest_block = block
                adopted = True
        if adopted:
            node.logger.info(
                "Loaded head block #%s %s from its holder",
                block.height,
                head_hash.hex(),
            )

    node.logger.info("Latest block discovery stopped")
