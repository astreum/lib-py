from __future__ import annotations

import time
from typing import TYPE_CHECKING, Sequence

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.models.peer import Peer, add_peer, get_peer
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.handshake import (
    build_handshake,
    parse_handshake,
    verify_handshake,
)
from astreum.communication.models.ping import Ping
from astreum.consensus.block.holders import get_ping_holder
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
            head = node.latest_block
        if not isinstance(latest_block, (bytes, bytearray)) or len(latest_block) != 32:
            latest_block = None
        try:
            ping_payload = Ping(
                is_validator=bool(node.config.validation_public_key_bytes),
                difficulty=message_difficulty(node),
                latest_block=latest_block,
                holder=get_ping_holder(node, latest_block, head),
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

    try:
        host = addr[0]
    except Exception:
        return True

    window = node.config.message_timestamp_window
    handshake = parse_handshake(message)
    if handshake is None or not verify_handshake(
        message, handshake, time.time(), window
    ):
        node.logger.debug(
            "Handshake dropped from %s: bad length, reply_requested, time or signature",
            addr,
        )
        return True
    relay_key_bytes = handshake.relay_key
    handshake_timestamp = handshake.timestamp
    reply_requested = handshake.reply_requested

    port = addr[1]
    peer_address = (host, port)
    default_seed_ips = node.default_seed_ips
    is_default_seed = bool(default_seed_ips) and host in default_seed_ips

    existing_peer = get_peer(node, storage_key_bytes)
    if existing_peer is not None:
        if relay_key_bytes != existing_peer.relay_public_key_bytes:
            if handshake_timestamp <= existing_peer.handshake_timestamp:
                node.logger.debug(
                    "Older handshake with another relay key dropped from %s",
                    peer_address,
                )
                return True
            try:
                existing_peer.set_relay_key(
                    node.relay_secret_key, relay_key_bytes, handshake_timestamp
                )
            except ValueError:
                return True
            node.logger.info("Relay key changed for peer at %s", peer_address)
        elif handshake_timestamp > existing_peer.handshake_timestamp:
            existing_peer.handshake_timestamp = handshake_timestamp
        if reply_requested:
            enqueue_outgoing(
                node,
                peer_address,
                message=build_handshake(node, reply_requested=False),
                difficulty=existing_peer.difficulty,
            )
        _queue_handshake_ping(existing_peer, peer_address)
        return True

    try:
        peer_relay_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
        peer = Peer(
            node_secret_key=node.relay_secret_key,
            peer_public_key=peer_relay_key,
            storage_public_key_bytes=storage_key_bytes,
            address=peer_address,
            is_default_seed=is_default_seed,
            handshake_timestamp=handshake_timestamp,
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
    response = build_handshake(node, reply_requested=False)
    enqueue_outgoing(
        node,
        peer_address,
        message=response,
        difficulty=peer.difficulty,
    )
    _queue_handshake_ping(peer, peer_address)
    return True
