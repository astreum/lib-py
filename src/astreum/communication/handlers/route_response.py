from __future__ import annotations

from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.models.message import Message
from astreum.communication.models.peer import get_peer
from astreum.communication.models.route_entry import decode_route_entries

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from astreum import Node
    from astreum.communication.models.peer import Peer


def _bucket_is_full(node: "Node", storage_key: bytes) -> bool:
    route = node.peer_route
    bucket = route.buckets.get(
        route._matching_leading_bits(route.storage_public_key_bytes, storage_key)
    )
    return bucket is not None and len(bucket) >= route.bucket_size


def handle_route_response(node: "Node", peer: "Peer", message: Message) -> None:
    """Send a handshake to each new node in a route reply.

    An entry is skipped if it names this node, a known peer, or (for route id
    0) a node whose bucket in ``peer_route`` is full.

    Args:
        node: The node that receives the reply.
        peer: The peer that sent the reply.
        message: The decrypted ``ROUTE_RESPONSE``.
    """
    payload = message.content
    if not payload:
        return
    route_id = payload[0]
    if route_id not in (0, 1):
        node.logger.warning("ROUTE_RESPONSE unknown route id %s from %s", route_id, peer.address)
        return
    try:
        entries = decode_route_entries(payload[1:], bool(node.use_ipv6))
    except ValueError:
        node.logger.warning(
            "ROUTE_RESPONSE payload size mismatch (%s bytes) from %s",
            len(payload),
            peer.address,
        )
        return

    targets = []
    for storage_key, _relay_key, address in entries:
        if storage_key == node.storage_public_key_bytes:
            continue
        if get_peer(node, storage_key) is not None:
            continue
        if route_id == 0 and _bucket_is_full(node, storage_key):
            continue
        targets.append(address)
    if not targets:
        return
    node.logger.debug("Handshaking %s new nodes from ROUTE_RESPONSE", len(targets))

    handshake_message = Message(
        handshake=True,
        sender_public_key_bytes=node.storage_public_key_bytes,
        content=node.relay_public_key_bytes,
    )
    for address in targets:
        enqueue_outgoing(
            node,
            address,
            message=handshake_message,
            difficulty=1,
        )
