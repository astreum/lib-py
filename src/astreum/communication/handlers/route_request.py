from __future__ import annotations

from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import get_peer
from astreum.communication.models.peer_info import PeerInfo, encode_peer_info
from astreum.communication.models.route_entry import max_route_entries
from astreum.communication.util import xor_distance

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astreum import Node
    from astreum.communication.models.peer import Peer
    from astreum.communication.models.route import Route


ROUTE_REQUEST_PLAIN_BYTES = 1
ROUTE_REQUEST_TARGET_BYTES = 1 + 32


def route_reply_entries(
    node: "Node", route: "Route", sender_key: bytes, rank_key: bytes
) -> list[bytes]:
    """Encode the peers of a route nearest to a key, nearest first.

    The sender is left out. The count fits one datagram and the bucket size.

    Args:
        node: The node that answers.
        route: The route to read the peers from.
        sender_key: The storage key of the node that asked.
        rank_key: The key to rank the peers by. This is the sender key for a
            plain request or the target for a targeted request.

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
            if peer.address is None:
                continue
            try:
                entry = encode_peer_info(
                    PeerInfo(peer.public_key_bytes, peer.relay_public_key_bytes, peer.address),
                    use_ipv6,
                )
            except ValueError:
                continue
            ranked.append((xor_distance(rank_key, peer_key), entry))
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
    if len(message.content) not in (ROUTE_REQUEST_PLAIN_BYTES, ROUTE_REQUEST_TARGET_BYTES):
        node.logger.debug(
            "ROUTE_REQUEST with bad length %s from %s", len(message.content), peer.address
        )
        return False, "bad length"
    route_id = message.content[0]
    rank_key = message.content[1:] if len(message.content) == ROUTE_REQUEST_TARGET_BYTES else sender_public_key
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

    entries = route_reply_entries(node, route, sender_public_key, rank_key)

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
