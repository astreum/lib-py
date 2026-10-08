"""Helpers related to disconnecting communication components."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum.node import Node


_SOCKET_ATTRS: tuple[str, ...] = ("socket",)
_THREAD_ATTRS: tuple[str, ...] = (
    "incoming_populate_thread",
    "incoming_process_thread",
    "outgoing_thread",
    "peer_manager_thread",
    "advertise_storage_thread",
    "claim_storage_thread",
    "latest_block_discovery_thread",
    "verify_thread",
    "consensus_validation_thread",
)


def _set_event(node: "Node", attr_name: str) -> None:
    """Set an event attribute on the node.

    Args:
        node (Node): The node with event attributes.
        attr_name (str): The name of the event attribute to set.

    Returns: None
    """
    event = getattr(node, attr_name, None)
    if event is not None:
        event.set()


def _close_socket(node: "Node", attr_name: str) -> None:
    """Close a socket attribute on the node.

    Args:
        node (Node): The node with socket attributes.
        attr_name (str): The name of the socket attribute to close.

    Returns: None
    """
    sock = getattr(node, attr_name, None)
    if sock is None:
        return
    try:
        sock.close()
    except Exception as exc:  # pragma: no cover - defensive logging path
        node.logger.warning("Error closing %s: %s", attr_name, exc)


def disconnect_node(node: "Node") -> None:
    """Gracefully stop worker threads and close communication sockets.

    Args:
        node (Node): The node to disconnect.

    Returns: None
    """
    node.logger.info("Disconnecting Astreum Node")

    _set_event(node, "communication_stop_event")
    _set_event(node, "_validation_stop_event")
    _set_event(node, "_verify_stop_event")

    for sock_name in _SOCKET_ATTRS:
        _close_socket(node, sock_name)

    for thread_name in _THREAD_ATTRS:
        thread = getattr(node, thread_name, None)
        if thread is None or not thread.is_alive():
            continue
        thread.join(timeout=1.0)

    node.is_connected = False
    node.logger.info("Node disconnected")
