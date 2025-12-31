from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..models.message import Message
from ..util import address_str_to_host_and_port

if TYPE_CHECKING:
    from .. import Node


def _queue_bootstrap_handshakes(node: "Node") -> int:
    outgoing_queue = getattr(node, "outgoing_queue", None)
    relay_public_key = getattr(node, "relay_public_key", None)
    if outgoing_queue is None or relay_public_key is None:
        return 0

    default_seeds = node.config.get("default_seeds", [])
    additional_seeds = node.config.get("additional_seeds", [])
    bootstrap_peers = list(default_seeds) + list(additional_seeds)
    if not bootstrap_peers:
        return 0

    try:
        incoming_port = int(node.config.get("incoming_port", 0))
        content = incoming_port.to_bytes(2, "big", signed=False)
    except (TypeError, ValueError, OverflowError):
        return 0

    handshake_message = Message(
        handshake=True,
        sender=relay_public_key,
        content=content,
    )
    handshake_bytes = handshake_message.to_bytes()
    sent = 0
    for addr in bootstrap_peers:
        try:
            host, port = address_str_to_host_and_port(addr)
        except Exception as exc:
            node.logger.warning("Invalid bootstrap address %s: %s", addr, exc)
            continue
        outgoing_queue.put((handshake_bytes, (host, port)))
        node.logger.info("Retrying bootstrap handshake to %s:%s", host, port)
        sent += 1
    return sent


def manage_peer(node: "Node") -> None:
    """Continuously evict peers whose timestamps exceed the configured timeout."""
    node.logger.info(
        "Peer manager started (timeout=%3ds, interval=%3ds)",
        node.config["peer_timeout"],
        node.config["peer_timeout_interval"],
    )
    stop = getattr(node, "communication_stop_event", None)
    while stop is None or not stop.is_set():
        timeout_seconds = node.config["peer_timeout"]
        interval_seconds = node.config["peer_timeout_interval"]
        try:
            peers = getattr(node, "peers", None)
            peer_route = getattr(node, "peer_route", None)
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
                removed = node.remove_peer(peer_key)
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
                peer_count = len(getattr(node, "peers", {}) or {})
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

        if stop is not None and stop.wait(interval_seconds):
            break

    node.logger.info("Peer manager stopped")
