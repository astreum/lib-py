from __future__ import annotations

from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import get_peer
from astreum.communication.models.route_entry import (
    encode_route_entry,
    max_route_entries,
)
from astreum.communication.util import xor_distance

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astreum import Node
    from astreum.communication.models.peer import Peer
    from astreum.communication.models.route import Route


def route_reply_entries(node: "Node", route: "Route", sender_key: bytes) -> list[bytes]:
    """Encode the peers of a route nearest to the sender, nearest first.

    The sender is left out. The count fits one datagram and the bucket size.

    Args:
        node: The node that answers.
        route: The route to read the peers from.
        sender_key: The storage key of the node that asked.

    Returns:
        The encoded entries, nearest peer first.
    """
    use_ipv6 = bool(node.use_ipv6)
    ranked = []
    for bucket in route.buckets.values():
        for peer_key in bucket:
            if peer_key == sender_key:
                continue
            peer = get_peer(node, peer_key)
            if peer is None:
                continue
            entry = encode_route_entry(peer, use_ipv6)
            if entry is None:
                continue
            ranked.append((xor_distance(sender_key, peer_key), entry))
    ranked.sort(key=lambda item: item[0])
    limit = max_route_entries(use_ipv6, route.bucket_size)
    return [entry for _, entry in ranked[:limit]]


def handle_route_request(node: "Node", peer: "Peer", message: Message) -> tuple[bool, str | None]:
    sender_public_key = getattr(peer, "public_key_bytes", None)
    if not sender_public_key:
        node.logger.debug("Unknown sender for ROUTE_REQUEST from %s", peer.address)
        return False, "unknown sender"

    if not message.content:
        node.logger.debug("ROUTE_REQUEST missing route id from %s", peer.address)
        return False, "missing route id"
    route_id = message.content[0]
    if route_id == 0:
        route = node.peer_route
    elif route_id == 1:
        route = node.validation_route
        if route is None:
            node.logger.debug("Validation route not initialized for %s", peer.address)
            return False, "validation route not initialized"
    else:
        node.logger.debug("Unknown route id %s in ROUTE_REQUEST from %s", route_id, peer.address)
        return False, f"unknown route id {route_id}"

    entries = route_reply_entries(node, route, sender_public_key)

    response = Message(
        topic=MessageTopic.ROUTE_RESPONSE,
        content=bytes([route_id]) + b"".join(entries),
        sender_public_key_bytes=node.storage_public_key_bytes,
    )
    response.encrypt(peer.shared_key_bytes)
    enqueue_outgoing(
        node,
        peer.address,
        message=response,
        difficulty=peer.difficulty,
    )
    return True, None
