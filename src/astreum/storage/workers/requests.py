"""Request buffer: STORAGE_GETs waiting to be batched and sent.

Not to be confused with ``astreum.storage.requests``, the pending-request
registry (``claim_expr_req`` and friends). This module only holds GETs that
have not been sent yet.

Requesters call :func:`queue_storage_get`; ``request_storage_thread`` runs
:func:`request_storage`, which every ``storage_request_flush_interval`` (or as
soon as one destination fills a datagram) sends one ``STORAGE_GET`` per
destination per chunk of ``max_get_entries`` exprs.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

from astreum.storage.requests import claim_expr_req, has_expr_req, pop_expr_req
from astreum.utils.config import (
    DEFAULT_STORAGE_GET_BATCH_MAX_BYTES,
    DEFAULT_STORAGE_REQUEST_FLUSH_INTERVAL_SECONDS,
)

if TYPE_CHECKING:
    from astreum import Node


class _Destination:
    """Pending GETs for one address: who to send to and how."""

    __slots__ = ("address", "shared_key_bytes", "difficulty", "entries")

    def __init__(self, address, shared_key_bytes: bytes, difficulty: int):
        self.address = address
        self.shared_key_bytes = shared_key_bytes
        self.difficulty = difficulty
        # expr_id -> (resolution, preclaimed)
        self.entries: dict[bytes, tuple[int, bool]] = {}


def init_request_buffer(node: "Node") -> None:
    """Create the buffer state on *node* (called from communication setup)."""
    node.storage_request_buffer = {}
    node.storage_request_buffered = set()
    node.storage_request_buffer_lock = threading.Lock()
    node.storage_request_flush_event = threading.Event()


def _provider_destination(node: "Node", relay_key_bytes: bytes, address: str, port: int):
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    provider_public_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
    shared_key_bytes = node.relay_secret_key.exchange(provider_public_key)
    return ("provider", relay_key_bytes, address, port), (address, port), shared_key_bytes, 1


def _resolve_destination(node: "Node", expr_id: bytes, contact=None):
    """Return ``(key, address, shared_key_bytes, difficulty)`` or ``None``.

    *contact* (``(relay_key, address, port)``) overrides routing, for redirects.
    Otherwise an indexed provider wins, then the closest peer.
    """
    try:
        if contact is not None:
            relay_key_bytes, address, port = contact
            return _provider_destination(node, relay_key_bytes, address, port)

        provider_id = node.storage_index.get(expr_id)
        if provider_id is not None:
            from astreum.communication.storage_response.storage_provider import (
                decode_storage_provider,
            )
            from astreum.storage.providers import provider_payload_for_id

            payload = provider_payload_for_id(node, provider_id)
            if payload is None:
                node.logger.debug("Unknown provider id %s for %s", provider_id, expr_id.hex())
                return None
            _, relay_key_bytes, address, port = decode_storage_provider(payload)
            return _provider_destination(node, relay_key_bytes, address, port)

        peer = node.peer_route.closest_peer_for_hash(expr_id)
        if peer is None or peer.address is None:
            return None
        return ("peer", peer.public_key_bytes), peer.address, peer.shared_key_bytes, peer.difficulty
    except Exception as exc:
        node.logger.debug("Destination lookup failed for %s: %s", expr_id.hex(), exc)
        return None


def _max_entries(node: "Node") -> int:
    from astreum.communication.storage_request.model import max_get_entries

    budget = (getattr(node, "config", None) or {}).get(
        "storage_get_batch_max_bytes", DEFAULT_STORAGE_GET_BATCH_MAX_BYTES
    )
    return max(1, max_get_entries(budget))


def queue_storage_get(
    node: "Node",
    expr_id: bytes,
    resolution: int,
    *,
    contact=None,
    preclaimed: bool = False,
) -> bool:
    """Buffer a GET for *expr_id*; the flush thread sends it.

    Returns False when there is nowhere to send it (no provider or peer), so
    the caller can give up instead of waiting. True means the GET is buffered,
    or one is already buffered or in flight and the caller should just poll.

    Args:
        contact: ``(relay_key, address, port)`` to send to instead of the
            routed destination (redirects and hints).
        preclaimed: The pending request was already claimed (redirect retries),
            so the flush must not claim it again.
    """
    with node.storage_request_buffer_lock:
        if expr_id in node.storage_request_buffered:
            return True
    if not preclaimed and has_expr_req(node, expr_id):
        return True

    resolved = _resolve_destination(node, expr_id, contact)
    if resolved is None:
        return False
    key, address, shared_key_bytes, difficulty = resolved

    with node.storage_request_buffer_lock:
        if expr_id in node.storage_request_buffered:
            return True
        destination = node.storage_request_buffer.get(key)
        if destination is None:
            destination = _Destination(address, shared_key_bytes, difficulty)
            node.storage_request_buffer[key] = destination
        destination.entries[expr_id] = (resolution, preclaimed)
        node.storage_request_buffered.add(expr_id)
        full = len(destination.entries) >= _max_entries(node)
    if full:
        node.storage_request_flush_event.set()
    return True


def _send_chunk(node: "Node", destination: _Destination, chunk: list[tuple[bytes, int]]) -> bool:
    from astreum.communication.models.message import Message, MessageTopic
    from astreum.communication.outgoing_queue import enqueue_outgoing
    from astreum.communication.storage_request.code import StorageRequestCode
    from astreum.communication.storage_request.model import StorageRequest

    request = StorageRequest(StorageRequestCode.STORAGE_GET, entries=chunk)
    message = Message(
        topic=MessageTopic.STORAGE_REQUEST,
        content=request.to_bytes(),
        sender_public_key_bytes=node.storage_public_key_bytes,
    )
    message.encrypt(destination.shared_key_bytes)
    return bool(
        enqueue_outgoing(
            node,
            destination.address,
            message=message,
            difficulty=destination.difficulty,
        )
    )


def flush_storage_requests(node: "Node") -> int:
    """Send everything buffered. Returns the number of datagrams queued.

    Destinations go lowest difficulty first so a busy peer's proof-of-work
    does not hold up the rest. Claims are taken here, right before sending.
    """
    with node.storage_request_buffer_lock:
        pending = node.storage_request_buffer
        node.storage_request_buffer = {}
        node.storage_request_buffered.clear()
    if not pending:
        return 0

    per_datagram = _max_entries(node)
    sent = 0
    for destination in sorted(pending.values(), key=lambda d: d.difficulty):
        live: list[tuple[bytes, int]] = []
        for expr_id, (resolution, preclaimed) in destination.entries.items():
            if preclaimed:
                if not has_expr_req(node, expr_id):
                    continue
            elif not claim_expr_req(node, expr_id, resolution):
                continue
            live.append((expr_id, resolution))

        for start in range(0, len(live), per_datagram):
            chunk = live[start : start + per_datagram]
            try:
                queued = _send_chunk(node, destination, chunk)
            except Exception as exc:
                node.logger.debug(
                    "STORAGE_GET of %d exprs to %s failed: %s",
                    len(chunk),
                    destination.address,
                    exc,
                )
                queued = False
            if queued:
                sent += 1
                node.logger.debug(
                    "Queued STORAGE_GET of %d exprs to %s", len(chunk), destination.address
                )
            else:
                for expr_id, _ in chunk:
                    pop_expr_req(node, expr_id)
    return sent


def request_storage(node: "Node") -> None:
    """Flush the request buffer on a timer until ``communication_stop_event``.

    Runs as ``request_storage_thread``. A destination filling a datagram sets
    ``storage_request_flush_event`` to flush early.
    """
    stop = node.communication_stop_event
    event = node.storage_request_flush_event
    interval = float(
        (getattr(node, "config", None) or {}).get(
            "storage_request_flush_interval",
            DEFAULT_STORAGE_REQUEST_FLUSH_INTERVAL_SECONDS,
        )
    )
    node.logger.info("Storage request flusher started (interval=%ss)", interval)
    while not stop.is_set():
        event.wait(interval)
        event.clear()
        if stop.is_set():
            break
        try:
            flush_storage_requests(node)
        except Exception as exc:
            node.logger.exception("Storage request flush failed: %s", exc)
    node.logger.info("Storage request flusher stopped")
