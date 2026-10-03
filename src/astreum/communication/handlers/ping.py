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
