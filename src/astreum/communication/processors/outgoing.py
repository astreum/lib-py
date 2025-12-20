from __future__ import annotations

from queue import Empty
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from .. import Node

def process_outgoing_messages(node: "Node") -> None:
    """Send queued outbound packets."""
    stop = getattr(node, "communication_stop_event", None)
    while stop is None or not stop.is_set():
        try:
            payload, addr = node.outgoing_queue.get(timeout=0.5)
        except Empty:
            continue
        except Exception:
            node.logger.exception("Error taking from outgoing queue")
            continue

        if stop is not None and stop.is_set():
            break

        try:
            node.outgoing_socket.sendto(payload, addr)
        except Exception as exc:
            node.logger.warning("Error sending message to %s: %s", addr, exc)

    node.logger.info("Outgoing message processor stopped")
