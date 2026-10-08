from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.models.peer import Peer, add_peer, get_peer
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.ping import Ping
from astreum.communication.difficulty import message_difficulty

if TYPE_CHECKING:
    from astreum import Node


def handle_handshake(node: "Node", addr: Sequence[object], message: Message) -> bool:
    """Handle incoming handshake messages.

    Returns True if the outer loop should `continue`, False otherwise.

    Args:
        node (Node): The node handling the handshake.
        addr (Sequence[object]): The address tuple (host, port).
        message (Message): The handshake message.

    Returns:
        bool: Description of the return value.
    """
    def _queue_handshake_ping(peer: Peer, peer_address: tuple[str, int]) -> None:
        with node.latest_block_lock:
            latest_block = node.latest_block_hash
        if not isinstance(latest_block, (bytes, bytearray)) or len(latest_block) != 32:
            latest_block = None
        try:
            ping_payload = Ping(
                is_validator=bool(node.config.get("validation_public_key_bytes")),
                difficulty=message_difficulty(node),
                latest_block=latest_block,
            ).to_bytes()
            ping_msg = Message(
                topic=MessageTopic.PING,
                content=ping_payload,
                sender_public_key_bytes=node.storage_public_key_bytes,
            )
            ping_msg.encrypt(peer.shared_key_bytes)
            enqueue_outgoing(
                node,
                peer_address,
                message=ping_msg,
                difficulty=peer.difficulty,
            )
        except Exception as exc:
            node.logger.debug(
                "Failed sending handshake ping to %s:%s: %s",
                peer_address[0],
                peer_address[1],
                exc,
            )
    storage_key_bytes = message.sender_public_key_bytes
    # Handshake content carries the X25519 relay public key (32 bytes) for DH
    relay_key_bytes = message.content[:32] if len(message.content) >= 32 else b""

    try:
        host = addr[0]
    except Exception:
        return True

    port = addr[1]
    peer_address = (host, port)
    default_seed_ips = node.default_seed_ips
    is_default_seed = bool(default_seed_ips) and host in default_seed_ips

    existing_peer = get_peer(node, storage_key_bytes)
    if existing_peer is not None:
        existing_peer.address = peer_address
        existing_peer.is_default_seed = is_default_seed
        _queue_handshake_ping(existing_peer, peer_address)
        return False

    try:
        peer_relay_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
        peer = Peer(
            node_secret_key=node.relay_secret_key,
            peer_public_key=peer_relay_key,
            storage_public_key_bytes=storage_key_bytes,
            address=peer_address,
            is_default_seed=is_default_seed,
        )
    except Exception:
        return True

    add_peer(node, storage_key_bytes, peer)
    node.peer_route.add_peer(storage_key_bytes, peer)

    node.logger.info(
        "Handshake accepted from %s:%s; peer added",
        peer_address[0],
        peer_address[1],
    )
    response = Message(
        handshake=True,
        sender_public_key_bytes=node.storage_public_key_bytes,
        content=node.relay_public_key_bytes,
    )
    enqueue_outgoing(
        node,
        peer_address,
        message=response,
        difficulty=peer.difficulty,
    )
    _queue_handshake_ping(peer, peer_address)
    return True
