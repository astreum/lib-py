from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from astreum.communication.models.ping import Ping
from astreum.communication.models.peer import Peer

if TYPE_CHECKING:
    from astreum import Node


def handle_ping(node: "Node", peer: Peer, payload: bytes) -> None:
    """Update peer and validation state based on an incoming ping message."""
    try:
        ping = Ping.from_bytes(payload)
    except Exception as exc:
        node.logger.warning("Error decoding ping: %s", exc)
        return

    peer.timestamp = datetime.now(timezone.utc)
    peer.latest_block = ping.latest_block
    peer.difficulty = ping.difficulty
    with node.latest_block_lock:
        if peer.is_default_seed and ping.latest_block:
            if node.latest_block_hash != ping.latest_block:
                node.latest_block_hash = ping.latest_block
                node.latest_block = None
            node.logger.info(
                "Updated latest block hash from default seed %s",
                peer.address[0] if peer.address else "unknown",
            )

    _request_uncommitted_block(node, peer, ping)

    validation_route = node.validation_route
    if validation_route is None:
        return

    try:
        if ping.is_validator:
            validation_route.add_peer(peer.public_key_bytes)
        else:
            validation_route.remove_peer(peer.public_key_bytes)
    except Exception:
        pass


def _request_uncommitted_block(node: "Node", peer: Peer, ping: Ping) -> None:
    """Ask a validator for the newest block it announced, if we don't have it.

    A validator's newest block is uncommitted, so its new exprs are only on that
    validator. The GET goes straight to it (``RESOLUTION_RECORD``; it answers
    with the block followed by its new exprs). Only buffers the request, so it
    is safe in the message thread.
    """
    block_hash = ping.latest_block
    if not ping.is_validator or not block_hash or peer.address is None:
        return
    try:
        from astreum.expression import RESOLUTION_RECORD
        from astreum.storage.exprs import get_expr_from_local_storage
        from astreum.storage.workers.requests import queue_storage_get

        if get_expr_from_local_storage(node, block_hash) is not None:
            return
        queue_storage_get(
            node,
            block_hash,
            RESOLUTION_RECORD,
            contact=(peer.relay_public_key_bytes, peer.address[0], peer.address[1]),
        )
    except Exception as exc:
        node.logger.debug("Uncommitted block request failed: %s", exc)
