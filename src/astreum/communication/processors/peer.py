from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .. import Node


def manage_peer(node: "Node") -> None:
    """Continuously evict peers whose timestamps exceed the configured timeout."""
    node.logger.info(
        "Peer manager started (timeout=%3ds, interval=%3ds)",
        node.config["peer_timeout"],
        node.config["peer_timeout_interval"],
    )
    while True:
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
        except Exception:
            node.logger.exception("Peer manager iteration failed")

        time.sleep(interval_seconds)
