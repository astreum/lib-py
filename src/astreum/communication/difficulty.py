from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astreum import Node


def message_difficulty(node: "Node") -> int:
    """Compute current message difficulty based on incoming queue pressure.

    Args:
        node (Node): The node with an incoming queue to measure.
    """
    size = node.incoming_queue_size
    limit = node.incoming_queue_size_limit

    if limit <= 0:
        return 1

    pressure = size / limit
    if pressure < 0.70:
        value = 1
    elif pressure < 0.75:
        value = 3
    elif pressure < 0.80:
        value = 5
    elif pressure < 0.85:
        value = 8
    elif pressure < 0.90:
        value = 12
    elif pressure < 0.93:
        value = 16
    elif pressure < 0.95:
        value = 19
    elif pressure < 0.97:
        value = 22
    elif pressure < 0.98:
        value = 24
    else:
        value = 26

    return max(1, min(255, value))
