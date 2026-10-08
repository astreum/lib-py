from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from blake3 import blake3

from astreum.communication.difficulty import message_difficulty
from astreum.communication.message_pow import NONCE_SIZE, _leading_zero_bits

if TYPE_CHECKING:
    from astreum import Node


INCOMING_QUEUE_ITEM_OVERHEAD_BYTES = 6


def enqueue_incoming(
    node: "Node",
    address: Tuple[str, int],
    payload: bytes,
) -> tuple[bool, str | None]:
    """Enqueue an incoming UDP payload while tracking queued bytes.

    Increments `node.incoming_queue_size` by `len(payload) + 6` and enforces
    `node.incoming_queue_size_limit` (bytes) as a soft cap by dropping enqueues that
    would exceed the limit. If `node.incoming_queue_timeout` is > 0, it waits up to
    that many seconds (using `communication_stop_event.wait`) for space before dropping.

    Args:
        node (Node): The node instance that owns the incoming queue.
        address (Tuple[str, int]): The source address as (host, port).
        payload (bytes): The raw UDP payload to enqueue.

    Returns:
        tuple[bool, str | None]: A tuple of (success, error_reason).
    """
    required_difficulty = message_difficulty(node)
    if len(payload) <= NONCE_SIZE:
        node.logger.debug(
            "Incoming payload too short for difficulty check (len=%s, required=%s)",
            len(payload),
            required_difficulty,
        )
        return False, "payload too short for difficulty check"

    nonce_bytes = payload[:NONCE_SIZE]
    message_bytes = payload[NONCE_SIZE:]
    digest = blake3(message_bytes + nonce_bytes).digest()
    zeros = _leading_zero_bits(digest)
    if zeros < required_difficulty:
        node.logger.debug(
            "Incoming payload failed difficulty check (zeros=%s required=%s bytes=%s)",
            zeros,
            required_difficulty,
            len(payload),
        )
        return False, "difficulty check failed"

    accounted_size = len(payload) + INCOMING_QUEUE_ITEM_OVERHEAD_BYTES
    timeout = float(node.incoming_queue_timeout or 0)

    with node.incoming_queue_size_lock:
        current_size = int(node.incoming_queue_size)
        limit = int(node.incoming_queue_size_limit)
        projected_size = current_size + accounted_size
        if projected_size > limit:
            if timeout <= 0:
                node.logger.debug(
                    "Incoming queue size limit reached (%s > %s); dropping inbound payload (bytes=%s)",
                    projected_size,
                    limit,
                    len(payload),
                )
                return False, "incoming queue size limit reached"
            wait_for_space = True
        else:
            node.incoming_queue_size = projected_size
            wait_for_space = False

    if wait_for_space:
        if node.communication_stop_event.wait(timeout):
            return False, "stopped while waiting for queue space"
        with node.incoming_queue_size_lock:
            current_size = int(node.incoming_queue_size)
            limit = int(node.incoming_queue_size_limit)
            projected_size = current_size + accounted_size
            if projected_size > limit:
                node.logger.debug(
                    "Incoming queue still full after waiting %ss (%s > %s); dropping inbound payload (bytes=%s)",
                    timeout,
                    projected_size,
                    limit,
                    len(payload),
                )
                return False, "incoming queue still full after waiting"
            node.incoming_queue_size = projected_size

    try:
        node.incoming_queue.put((message_bytes, address, accounted_size, len(payload)))
    except Exception:
        with node.incoming_queue_size_lock:
            node.incoming_queue_size = max(0, int(node.incoming_queue_size) - accounted_size)
        raise

    return True, None
