from __future__ import annotations

import time
from queue import Empty
from typing import Any, Set

from astreum.validation.models.fork import Fork


def _process_peers_latest_block(
    node: Any, latest_block_hash: bytes, peer_ids: Set[Any]
) -> None:
    """Assign peers to the fork that matches their reported head."""
    node.logger.debug(
        "Processing %d peers reporting block %s",
        len(peer_ids),
        latest_block_hash.hex()
        if isinstance(latest_block_hash, (bytes, bytearray))
        else latest_block_hash,
    )
    new_fork = Fork(head=latest_block_hash)

    current_fork_heads = {
        fk.head for fk in node.forks.values() if fk.head != latest_block_hash
    }

    new_fork.validate(storage_get=node.storage_get, stop_heads=current_fork_heads)

    if new_fork.validated_upto and new_fork.validated_upto in node.forks:
        ref = node.forks[new_fork.validated_upto]
        if getattr(ref, "malicious_block_hash", None):
            node.logger.warning(
                "Skipping fork from block %s referencing malicious fork %s",
                latest_block_hash.hex()
                if isinstance(latest_block_hash, (bytes, bytearray))
                else latest_block_hash,
                new_fork.validated_upto.hex()
                if isinstance(new_fork.validated_upto, (bytes, bytearray))
                else new_fork.validated_upto,
            )
            return
        new_fork.root = ref.root
        new_fork.validated_upto = ref.validated_upto
        new_fork.chain_fork_position = ref.chain_fork_position

    for peer_id in peer_ids:
        new_fork.add_peer(peer_id)
        for head, fork in list(node.forks.items()):
            if head != latest_block_hash:
                fork.remove_peer(peer_id)

    node.forks[latest_block_hash] = new_fork
    node.logger.debug(
        "Fork %s now has %d peers (total forks %d)",
        latest_block_hash.hex()
        if isinstance(latest_block_hash, (bytes, bytearray))
        else latest_block_hash,
        len(new_fork.peers),
        len(node.forks),
    )


def make_verify_worker(node: Any):
    """Build the verify worker bound to the given node."""

    def _verify_worker() -> None:
        node.logger.info("Verify worker started")
        stop = node._verify_stop_event
        while not stop.is_set():
            batch: list[tuple[bytes, Set[Any]]] = []
            try:
                while True:
                    latest_b, peers = node._validation_verify_queue.get_nowait()
                    batch.append((latest_b, peers))
            except Empty:
                pass

            if not batch:
                node.logger.debug("Verify queue empty; sleeping")
                time.sleep(0.1)
                continue

            for latest_b, peers in batch:
                try:
                    _process_peers_latest_block(node, latest_b, peers)
                    node.logger.debug(
                        "Updated forks from block %s for %d peers",
                        latest_b.hex()
                        if isinstance(latest_b, (bytes, bytearray))
                        else latest_b,
                        len(peers),
                    )
                except Exception:
                    latest_hex = (
                        latest_b.hex()
                        if isinstance(latest_b, (bytes, bytearray))
                        else latest_b
                    )
                    node.logger.exception(
                        "Failed processing verification batch for %s", latest_hex
                    )
        node.logger.info("Verify worker stopped")

    return _verify_worker
