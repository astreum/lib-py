from __future__ import annotations

import socket
from datetime import datetime, timezone
from queue import Empty
from time import time
from typing import TYPE_CHECKING

from astreum.communication.handlers.handshake import handle_handshake
from astreum.communication.storage_request.handle import handle_storage_request
from astreum.communication.storage_response.handle import handle_storage_response
from astreum.communication.handlers.block import handle_block
from astreum.communication.handlers.ping import handle_ping
from astreum.communication.handlers.route_request import handle_route_request
from astreum.communication.handlers.route_response import handle_route_response
from astreum.communication.incoming_queue import enqueue_incoming
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.handshake import build_handshake
from astreum.communication.models.peer import Peer, get_peer, increment_peer_metric
from astreum.communication.outgoing_queue import enqueue_outgoing

if TYPE_CHECKING:
    from astreum.communication import Node


def process_incoming_messages(node: "Node") -> None:
    """Process incoming messages (placeholder).

    Args:
        node (Node): The node instance processing the messages.

    Returns: None
    """
    stop = node.communication_stop_event
    while not stop.is_set():
        try:
            item = node.incoming_queue.get(timeout=0.5)
        except Empty:
            continue
        except Exception:
            node.logger.exception("Error taking from incoming queue")
            continue

        data = None
        addr = None
        accounted_size = None
        packet_size = None

        if isinstance(item, tuple) and len(item) == 4:
            data, addr, accounted_size, packet_size = item
        elif isinstance(item, tuple) and len(item) == 3:
            data, addr, accounted_size = item
        else:
            node.logger.warning("Incoming queue item has unexpected shape: %r", item)
            continue

        if stop.is_set():
            if accounted_size is not None:
                try:
                    with node.incoming_queue_size_lock:
                        node.incoming_queue_size = max(0, node.incoming_queue_size - int(accounted_size))
                except Exception:
                    node.logger.exception("Failed updating incoming_queue_size on shutdown")
            break

        try:
            message = Message.from_bytes(data)
        except Exception as exc:
            node.logger.warning("Error decoding message: %s", exc)
            continue

        if message.handshake:
            if handle_handshake(node, addr, message):
                try:
                    handshake_peer = get_peer(node, message.sender_public_key_bytes)
                except Exception:
                    handshake_peer = None
                if handshake_peer is not None and packet_size is not None:
                    increment_peer_metric(
                        handshake_peer,
                        "total_network_download",
                        packet_size,
                    )
                continue

        peer = None
        try:
            peer = get_peer(node, message.sender_public_key_bytes)
        except Exception:
            peer = None

        if peer is None:
            # Non-handshake messages require a prior handshake to establish the
            # X25519 relay key for DH.  Request a handshake if unknown.
            node.logger.debug("Unknown peer for non-handshake message from %s; requesting handshake", addr)
            try:
                host = addr[0]
                port = addr[1]
                handshake_message = build_handshake(node, reply_requested=True)
                enqueue_outgoing(
                    node,
                    (host, port),
                    message=handshake_message,
                    difficulty=1,
                )
            except Exception:
                pass
            continue

        if packet_size is not None:
            increment_peer_metric(peer, "total_network_download", packet_size)

        # decrypt message payload before dispatch
        try:
            message.decrypt(peer.shared_key_bytes)
        except Exception as exc:
            node.logger.warning(
                "Error decrypting message from %s (len=%s, enc_len=%s, exc=%s)",
                peer.address,
                len(data),
                len(message.encrypted) if message.encrypted is not None else None,
                exc,
            )
            try:
                host = addr[0]
                port = addr[1]
                handshake_message = build_handshake(node, reply_requested=True)
                enqueue_outgoing(
                    node,
                    (host, port),
                    message=handshake_message,
                    difficulty=1,
                )
            except Exception as handshake_exc:
                node.logger.debug(
                    "Failed queueing rekey handshake to %s: %s",
                    addr,
                    handshake_exc,
                )
            continue

        try:
            window = node.config.get("message_timestamp_window", 0)
            if window and abs(time() - message.timestamp) > window:
                node.logger.debug(
                    "Stale message dropped from %s (timestamp=%s, window=%s)",
                    peer.address,
                    message.timestamp,
                    window,
                )
                continue

            peer_address = (addr[0], addr[1])
            if peer.address != peer_address:
                peer.address = peer_address
                peer.is_default_seed = (
                    bool(node.default_seed_ips) and addr[0] in node.default_seed_ips
                )

            peer.timestamp = datetime.now(timezone.utc)

            match message.topic:
                case MessageTopic.PING:
                    handle_ping(node, peer, message.content)

                case MessageTopic.STORAGE_REQUEST:
                    handled, reason = handle_storage_request(node, peer, message)
                    if not handled:
                        node.logger.warning(
                            "STORAGE_REQUEST handling failed from=%s reason=%s",
                            peer.address,
                            reason,
                        )

                case MessageTopic.STORAGE_RESPONSE:
                    handled, reason = handle_storage_response(node, peer, message)
                    if not handled:
                        node.logger.warning(
                            "STORAGE_RESPONSE handling failed from=%s reason=%s",
                            peer.address,
                            reason,
                        )

                case MessageTopic.ROUTE_REQUEST:
                    handled, reason = handle_route_request(node, peer, message)
                    if not handled:
                        node.logger.warning(
                            "ROUTE_REQUEST handling failed from=%s reason=%s",
                            peer.address,
                            reason,
                        )

                case MessageTopic.ROUTE_RESPONSE:
                    handle_route_response(node, peer, message)

                case MessageTopic.TRANSACTION:
                    if node.config.get("validation_secret_key") is None:
                        continue
                    from astreum.consensus.transaction.message import decode_transaction_message
                    transaction = decode_transaction_message(message.content)
                    if transaction is None:
                        node.logger.debug(
                            "TRANSACTION dropped from %s: invalid payload",
                            peer.address,
                        )
                        continue
                    node._validation_transaction_queue.put(transaction)

                case MessageTopic.BLOCK:
                    handle_block(node, peer, message.content)

                case _:
                    continue
        finally:
            if accounted_size is not None:
                try:
                    with node.incoming_queue_size_lock:
                        node.incoming_queue_size = max(0, node.incoming_queue_size - int(accounted_size))
                except Exception:
                    node.logger.exception("Failed updating incoming_queue_size")

    node.logger.info("Incoming message processor stopped")


def populate_incoming_messages(node: "Node") -> None:
    """Receive UDP packets and feed the incoming queue.

    Args:
        node (Node): The node instance receiving UDP packets.

    Returns: None
    """
    stop = node.communication_stop_event
    while not stop.is_set():
        try:
            data, addr = node.socket.recvfrom(65535)
            enqueued, reason = enqueue_incoming(node, addr, payload=data)
            if not enqueued:
                node.logger.warning(
                    "Incoming payload enqueue failed from=%s reason=%s bytes=%s",
                    addr,
                    reason,
                    len(data),
                )
        except socket.timeout:
            continue
        except OSError:
            if stop.is_set():
                break
            node.logger.warning("Error populating incoming queue: socket closed")
        except Exception as exc:
            node.logger.warning("Error populating incoming queue: %s", exc)

    node.logger.info("Incoming message populator stopped")
