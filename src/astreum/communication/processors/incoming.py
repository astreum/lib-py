from __future__ import annotations

import socket
from queue import Empty
from typing import TYPE_CHECKING

from ..handlers.handshake import handle_handshake
from ..handlers.object_request import handle_object_request
from ..handlers.object_response import handle_object_response
from ..handlers.ping import handle_ping
from ..handlers.route_request import handle_route_request
from ..handlers.route_response import handle_route_response
from ..models.message import Message, MessageTopic
from ..models.peer import Peer
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

if TYPE_CHECKING:
    from .. import Node


def process_incoming_messages(node: "Node") -> None:
    """Process incoming messages (placeholder)."""
    stop = getattr(node, "communication_stop_event", None)
    while stop is None or not stop.is_set():
        try:
            data, addr = node.incoming_queue.get(timeout=0.5)
        except Empty:
            continue
        except Exception:
            node.logger.exception("Error taking from incoming queue")
            continue

        if stop is not None and stop.is_set():
            break

        try:
            message = Message.from_bytes(data)
        except Exception as exc:
            node.logger.warning("Error decoding message: %s", exc)
            continue

        if message.handshake:
            if handle_handshake(node, addr, message):
                continue
        
        peer = None
        try:
            peer = node.get_peer(message.sender_bytes)
        except Exception:
            peer = None
        if peer is None:
            try:
                peer_key = X25519PublicKey.from_public_bytes(message.sender_bytes)
                host, port = addr[0], int(addr[1])
                peer = Peer(
                    node_secret_key=node.relay_secret_key,
                    peer_public_key=peer_key,
                    address=(host, port),
                )
            except Exception:
                peer = None

        if peer is None:
            node.logger.debug("Unable to resolve peer for message from %s", addr)
            continue

        # decrypt message payload before dispatch
        try:
            message.decrypt(peer.shared_key_bytes)
        except Exception as exc:
            node.logger.warning("Error decrypting message from %s: %s", peer.address, exc)
            continue

        match message.topic:
            case MessageTopic.PING:
                handle_ping(node, peer, message.content)

            case MessageTopic.OBJECT_REQUEST:
                handle_object_request(node, peer, message)

            case MessageTopic.OBJECT_RESPONSE:
                handle_object_response(node, peer, message)

            case MessageTopic.ROUTE_REQUEST:
                handle_route_request(node, peer, message)

            case MessageTopic.ROUTE_RESPONSE:
                handle_route_response(node, peer, message)

            case MessageTopic.TRANSACTION:
                if node.validation_secret_key is None:
                    continue
                node._validation_transaction_queue.put(message.content)

            case _:
                continue

    node.logger.info("Incoming message processor stopped")


def populate_incoming_messages(node: "Node") -> None:
    """Receive UDP packets and feed the incoming queue."""
    stop = getattr(node, "communication_stop_event", None)
    while stop is None or not stop.is_set():
        try:
            data, addr = node.incoming_socket.recvfrom(4096)
            node.incoming_queue.put((data, addr))
        except socket.timeout:
            continue
        except OSError:
            if stop is not None and stop.is_set():
                break
            node.logger.warning("Error populating incoming queue: socket closed")
        except Exception as exc:
            node.logger.warning("Error populating incoming queue: %s", exc)

    node.logger.info("Incoming message populator stopped")
