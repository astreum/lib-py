from __future__ import annotations

from queue import Empty
from typing import TYPE_CHECKING

from astreum.communication.models.peer import increment_peer_metric

if TYPE_CHECKING:
    from astreum.communication import Node


def _find_peer_by_address(node: "Node", addr):
    """Find a peer by its address.

    Args:
        node (Node): The node instance containing the peer list.
        addr (Any): The address to search for as (host, port).

    Returns:
        Any: The matching peer object or None if not found.
    """
    try:
        host = addr[0]
        port = int(addr[1])
    except Exception:
        return None

    peers = node.peers
    if not isinstance(peers, dict):
        return None

    with node.peers_lock:
        peer_values = list(peers.values())

    for peer in peer_values:
        try:
            peer_addr = getattr(peer, "address", None)
            if peer_addr is None:
                continue
            if peer_addr[0] == host and int(peer_addr[1]) == port:
                return peer
        except Exception:
            continue
    return None

def process_outgoing_messages(node: "Node") -> None:
    """Send queued outbound packets.

    Args:
        node (Node): The node instance sending the packets.
    """
    stop = node.communication_stop_event
    while not stop.is_set():
        try:
            item = node.outgoing_queue.get(timeout=0.5)
        except Empty:
            continue
        except Exception:
            node.logger.exception("Error taking from outgoing queue")
            continue

        payload = None
        addr = None
        if isinstance(item, tuple) and len(item) == 2:
            payload, addr = item
        else:
            node.logger.warning("Outgoing queue item has unexpected shape: %r", item)
            continue

        if stop.is_set():
            break

        try:
            sent_bytes = node.socket.sendto(payload, addr)
            peer = _find_peer_by_address(node, addr)
            if peer is not None:
                increment_peer_metric(
                    peer,
                    "total_network_upload",
                    sent_bytes if sent_bytes is not None else len(payload),
                )
        except Exception as exc:
            node.logger.warning("Error sending message to %s: %s", addr, exc)

    node.logger.info("Outgoing message processor stopped")
