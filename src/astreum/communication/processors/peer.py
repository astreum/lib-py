from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from astreum.communication.models.handshake import build_handshake
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
    bootstrap_peers = node.bootstrap_peers
    if not bootstrap_peers:
        return 0

    handshake_message = build_handshake(node, reply_requested=True)
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


def _send_keep_alive(node: "Node", peer) -> None:
    """Queue one keep-alive handshake to a stale peer.

    Args:
        node (Node): The node that sends the handshake.
        peer (Peer): The stale peer. Its address must not be None.

    Returns: None
    """
    try:
        enqueue_outgoing(
            node,
            peer.address,
            message=build_handshake(node, reply_requested=False),
            difficulty=1,
        )
    except Exception as exc:
        node.logger.debug("Failed queueing keep-alive handshake: %s", exc)


def _remove_stale_peer(node: "Node", peer_key: bytes) -> bool:
    """Remove a peer from the peer table and the route.

    Args:
        node (Node): The node that owns the peer.
        peer_key (bytes): Storage key of the peer.

    Returns:
        bool: True if the peer was in the table, False otherwise.
    """
    removed = remove_peer(node, peer_key)
    if removed is None:
        return False
    try:
        node.peer_route.remove_peer(peer_key)
    except Exception:
        node.logger.debug("Unable to remove peer %s from route", peer_key.hex())
    node.logger.debug(
        "Evicted stale peer %s last seen at %s",
        peer_key.hex(),
        getattr(removed, "timestamp", None),
    )
    return True


def _manage_peer_pass(node: "Node", probed: dict, now: float) -> None:
    """Run one pass of the peer manager.

    A stale peer gets one keep-alive handshake. The pass removes the peer only
    if it is still stale two ``peer_timeout_interval`` after the handshake.

    Args:
        node (Node): The node that manages its peers.
        probed (dict[bytes, float]): Monotonic time of the keep-alive for each
            stale peer key. The pass changes it.
        now (float): The current monotonic time in seconds.

    Returns: None
    """
    peers = node.peers
    peer_route = node.peer_route
    if not isinstance(peers, dict) or peer_route is None:
        return

    timeout_seconds = node.config.peer_timeout
    wait_seconds = 2 * node.config.peer_timeout_interval
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    with node.peers_lock:
        stale = {key: peer for key, peer in peers.items() if peer.timestamp < cutoff}

    removed_count = 0
    for peer_key, peer in stale.items():
        if peer.address is None:
            if _remove_stale_peer(node, peer_key):
                removed_count += 1
            probed.pop(peer_key, None)
            continue
        sent_at = probed.get(peer_key)
        if sent_at is None:
            _send_keep_alive(node, peer)
            probed[peer_key] = now
        elif now - sent_at >= wait_seconds:
            if _remove_stale_peer(node, peer_key):
                removed_count += 1
            probed.pop(peer_key, None)

    for peer_key in [key for key in probed if key not in stale]:
        probed.pop(peer_key)

    if removed_count:
        node.logger.info("Peer manager removed %s stale peer(s)", removed_count)

    with node.peers_lock:
        peer_count = len(peers)
    if peer_count == 0:
        bootstrap_interval = node.config.bootstrap_retry_interval
        wall_now = time.time()
        last_attempt = getattr(node, "_bootstrap_last_attempt", 0.0)
        if bootstrap_interval and (wall_now - last_attempt) >= bootstrap_interval:
            sent = _queue_bootstrap_handshakes(node)
            if sent:
                node._bootstrap_last_attempt = wall_now


def manage_peer(node: "Node") -> None:
    """Continuously probe and evict peers whose timestamps exceed the timeout.

    Args:
        node (Node): The node instance managing peers.

    Returns: None
    """
    node.logger.info(
        "Peer manager started (timeout=%3ds, interval=%3ds)",
        node.config.peer_timeout,
        node.config.peer_timeout_interval,
    )
    stop = node.communication_stop_event
    probed: dict[bytes, float] = {}
    while not stop.is_set():
        interval_seconds = node.config.peer_timeout_interval
        try:
            _manage_peer_pass(node, probed, time.monotonic())
        except Exception:
            node.logger.exception("Peer manager iteration failed")

        if stop.wait(interval_seconds):
            break

    node.logger.info("Peer manager stopped")
