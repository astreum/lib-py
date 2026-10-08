from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from astreum.communication.models.message import Message
from astreum.communication.models.peer import remove_peer
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.communication.util import address_str_to_host_and_port

if TYPE_CHECKING:
    from astreum.communication import Node


def _queue_bootstrap_handshakes(node: "Node") -> int:
    """Queue handshake messages to all bootstrap peers.

    Args:
        node (Node): The node instance initiating handshakes.

    Returns:
        int: The number of handshakes successfully queued.
    """
    relay_public_key = node.relay_public_key

    bootstrap_peers = node.bootstrap_peers
    if not bootstrap_peers:
        return 0

    handshake_message = Message(
        handshake=True,
        sender=relay_public_key,
        content=node.relay_public_key_bytes,
    )
    sent = 0
    for addr in bootstrap_peers:
        try:
            host, port = address_str_to_host_and_port(addr)
        except Exception as exc:
            node.logger.warning("Invalid bootstrap address %s: %s", addr, exc)
            continue
        try:
            queued = enqueue_outgoing(
                node,
                (host, port),
                message=handshake_message,
                difficulty=1,
            )
        except Exception as exc:
            node.logger.debug(
                "Failed queueing bootstrap handshake to %s:%s: %s",
                host,
                port,
                exc,
            )
            continue
        if queued:
            node.logger.info("Retrying bootstrap handshake to %s:%s", host, port)
            sent += 1
        else:
            node.logger.debug(
                "Bootstrap handshake queue rejected for %s:%s",
                host,
                port,
            )
    return sent


def manage_peer(node: "Node") -> None:
    """Continuously evict peers whose timestamps exceed the configured timeout.

    Args:
        node (Node): The node instance managing peers.
    """
    node.logger.info(
        "Peer manager started (timeout=%3ds, interval=%3ds)",
        node.config["peer_timeout"],
        node.config["peer_timeout_interval"],
    )
    stop = node.communication_stop_event
    while not stop.is_set():
        timeout_seconds = node.config["peer_timeout"]
        interval_seconds = node.config["peer_timeout_interval"]
        try:
            peers = node.peers
            peer_route = node.peer_route
            if not isinstance(peers, dict) or peer_route is None:
                time.sleep(interval_seconds)
                continue

            cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
            stale_keys = []
            with node.peers_lock:
                for peer_key, peer in list(peers.items()):
                    if peer.timestamp < cutoff:
                        stale_keys.append(peer_key)

            removed_count = 0
            for peer_key in stale_keys:
                removed = remove_peer(node, peer_key)
                if removed is None:
                    continue
                removed_count += 1
                try:
                    peer_route.remove_peer(peer_key)
                except Exception:
                    node.logger.debug(
                        "Unable to remove peer %s from route",
                        peer_key.hex(),
                    )
                node.logger.debug(
                    "Evicted stale peer %s last seen at %s",
                    peer_key.hex(),
                    getattr(removed, "timestamp", None),
                )

            if removed_count:
                node.logger.info("Peer manager removed %s stale peer(s)", removed_count)

            try:
                with node.peers_lock:
                    peer_count = len(peers)
            except Exception:
                peer_count = len(node.peers)
            if peer_count == 0:
                bootstrap_interval = node.config.get("bootstrap_retry_interval", 0)
                now = time.time()
                last_attempt = getattr(node, "_bootstrap_last_attempt", 0.0)
                if bootstrap_interval and (now - last_attempt) >= bootstrap_interval:
                    sent = _queue_bootstrap_handshakes(node)
                    if sent:
                        node._bootstrap_last_attempt = now
        except Exception:
            node.logger.exception("Peer manager iteration failed")

        if stop.wait(interval_seconds):
            break

    node.logger.info("Peer manager stopped")
