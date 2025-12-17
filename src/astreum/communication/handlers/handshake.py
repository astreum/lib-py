from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from ..models.peer import Peer
from ..models.message import Message

if TYPE_CHECKING:
    from .... import Node


def handle_handshake(node: "Node", addr: Sequence[object], message: Message) -> bool:
    """Handle incoming handshake messages.

    Returns True if the outer loop should `continue`, False otherwise.
    """
    sender_public_key_bytes = message.sender_bytes
    try:
        sender_key = X25519PublicKey.from_public_bytes(sender_public_key_bytes)
    except Exception as exc:
        node.logger.warning("Error extracting sender key bytes: %s", exc)
        return True

    try:
        host = addr[0]
        port = int.from_bytes(message.content[:2], "big", signed=False)
    except Exception:
        return True
    peer_address = (host, port)

    existing_peer = node.get_peer(sender_public_key_bytes)
    if existing_peer is not None:
        existing_peer.address = peer_address
        return False

    try:
        peer = Peer(
            node_secret_key=node.relay_secret_key,
            peer_public_key=sender_key,
            address=peer_address,
        )
    except Exception:
        return True

    node.add_peer(sender_public_key_bytes, peer)
    node.peer_route.add_peer(sender_public_key_bytes, peer)

    node.logger.info(
        "Handshake accepted from %s:%s; peer added",
        peer_address[0],
        peer_address[1],
    )
    response = Message(
        handshake=True,
        sender=node.relay_public_key,
        content=int(node.config["incoming_port"]).to_bytes(2, "big", signed=False),
    )
    node.outgoing_queue.put((response.to_bytes(), peer_address))
    return True
