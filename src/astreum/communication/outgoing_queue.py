from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from astreum.communication.message_pow import (
    MAX_UDP_DATAGRAM_BYTES,
    NONCE_SIZE,
    calculate_message_nonce,
)

if TYPE_CHECKING:
    from astreum.communication.models.message import Message
    from astreum import Node


def enqueue_outgoing(
    node: "Node",
    address: Tuple[str, int],
    message: "Message",
    difficulty: int = 1,
) -> bool:
    """Enqueue an outgoing UDP payload."""
    # if not node.is_connected:
    #     raise RuntimeError("node is not connected; call node.connect() (communication_setup) first")

    if message.sender_public_key_bytes is None:
        message.sender_public_key_bytes = node.config["relay_public_key_bytes"]

    payload = message.to_bytes()

    if NONCE_SIZE + len(payload) > MAX_UDP_DATAGRAM_BYTES:
        node.logger.warning(
            "Dropping oversized outgoing message (bytes=%s limit=%s address=%s topic=%s)",
            NONCE_SIZE + len(payload),
            MAX_UDP_DATAGRAM_BYTES,
            address,
            getattr(message, "topic", None),
        )
        return False

    try:
        difficulty_value = int(difficulty)
    except Exception:
        difficulty_value = 1
    if difficulty_value < 1:
        difficulty_value = 1

    try:
        nonce = calculate_message_nonce(payload, difficulty_value)
    except Exception as exc:
        node.logger.warning(
            "Failed generating message nonce (difficulty=%s bytes=%s): %s",
            difficulty_value,
            len(payload),
            exc,
        )
        return False

    payload = nonce.to_bytes(NONCE_SIZE, "big", signed=False) + payload

    node.outgoing_queue.put((payload, address))

    return True
