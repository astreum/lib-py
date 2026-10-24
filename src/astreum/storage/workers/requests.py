"""Request buffer: STORAGE_GETs waiting to be batched and sent.

Not to be confused with ``astreum.storage.requests``, the pending-request
registry (``claim_expr_req`` and friends). This module only holds GETs that
have not been sent yet.

Requesters call :func:`queue_storage_get`. ``request_storage_thread`` runs
:func:`request_storage`, which every ``storage_request_flush_interval`` (or as
soon as one destination fills a datagram) sends one ``STORAGE_GET`` per
destination per chunk of entries that fit the datagram. The chunk is counted
in bytes, because a shaped entry is longer than a plain one.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

from astreum.communication.storage_request.model import (
    SCOPED_GET_ENTRY_BYTES,
    get_entry_size,
    parse_radix_param,
)
from astreum.expression.expr import RESOLUTION_DYNAMIC, RESOLUTION_RADIX
from astreum.expression.shape import parse_shape
from astreum.storage.requests import claim_expr_req, has_expr_req, pop_expr_req

if TYPE_CHECKING:
    from astreum import Node
    from astreum.communication.models.relay_endpoint import RelayEndpoint


class _Destination:
    """Pending GETs for one address: who to send to and how."""

    __slots__ = ("address", "shared_key_bytes", "difficulty", "entries", "size")

    def __init__(self, address, shared_key_bytes: bytes, difficulty: int):
        self.address = address
        self.shared_key_bytes = shared_key_bytes
        self.difficulty = difficulty
        # expr_id -> (resolution, preclaimed, block_hash | None, shape)
        self.entries: dict[bytes, tuple[int, bool, bytes | None, bytes]] = {}
        # Encoded bytes of the buffered entries.
        self.size = 0


def init_request_buffer(node: "Node") -> None:
    """Create the buffer state on *node* (called from communication setup).

    Args:
        node: A Node instance to attach the buffer to.

    Returns: None
    """
    node.storage_request_buffer = {}
    node.storage_request_buffered = set()
    node.storage_request_buffer_lock = threading.Lock()
    node.storage_request_flush_event = threading.Event()


def _provider_destination(node: "Node", endpoint: "RelayEndpoint"):
    """Build a destination tuple for a provider.

    Args:
        node: A Node instance with relay secret key.
        endpoint: The provider's relay key and address.

    Returns:
        A tuple of (key, address, shared_key_bytes, difficulty).
    """
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    relay_key_bytes = endpoint.relay_key
    address, port = endpoint.address
    provider_public_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
    shared_key_bytes = node.relay_secret_key.exchange(provider_public_key)
    return ("provider", relay_key_bytes, address, port), (address, port), shared_key_bytes, 1


def _resolve_destination(node: "Node", expr_id: bytes, contact=None, block_hash=None):
    """Return ``(key, address, shared_key_bytes, difficulty)`` or ``None``.

    *contact* (a ``RelayEndpoint``) overrides routing, for redirects.
    A scoped request (*block_hash*) is routed by the block: its holder if there
    is one, else the provider indexed for it, else the peer nearest the block
    hash. Otherwise the nearest usable indexed provider wins for *expr_id*, then
    the closest peer.

    Args:
        node: A Node instance for routing.
        expr_id: The content hash to route to.
        contact: Optional ``RelayEndpoint`` for direct routing.
        block_hash: Optional block hash for scoped routing.

    Returns:
        A tuple of (key, address, shared_key_bytes, difficulty) or None.
    """
    try:
        if contact is not None:
            return _provider_destination(node, contact)

        route_id = expr_id
        if block_hash is not None:
            from astreum.consensus.block.holders import block_holder

            holder = block_holder(node, block_hash)
            if holder is not None:
                return _provider_destination(node, holder)
            route_id = block_hash

        for provider_id in node.storage_index.get(route_id, ()):
            from astreum.communication.models.peer_info import decode_peer_info
            from astreum.communication.models.relay_endpoint import RelayEndpoint
            from astreum.storage.providers import provider_payload_for_id

            payload = provider_payload_for_id(node, provider_id)
            if payload is None:
                node.logger.debug("Unknown provider id %s for %s", provider_id, route_id.hex())
                continue
            try:
                info = decode_peer_info(payload, False)
            except ValueError:
                continue
            return _provider_destination(node, RelayEndpoint(info.relay_key, info.address))

        peer = node.peer_route.closest_peer_for_hash(route_id)
        if peer is None or peer.address is None:
            return None
        return ("peer", peer.public_key_bytes), peer.address, peer.shared_key_bytes, peer.difficulty
    except Exception as exc:
        node.logger.debug("Destination lookup failed for %s: %s", expr_id.hex(), exc)
        return None


def _body_budget(node: "Node") -> int:
    """Return the entry bytes that fit in one STORAGE_GET datagram.

    Args:
        node: A Node instance with config.

    Returns:
        The number of bytes available for entries.
    """
    from astreum.communication.storage_request.model import get_body_budget

    return get_body_budget(node.config.storage_get_batch_max_bytes)


def queue_storage_get(
    node: "Node",
    expr_id: bytes,
    resolution: int,
    *,
    contact=None,
    preclaimed: bool = False,
    block_hash: Optional[bytes] = None,
    shape: bytes = b"",
) -> bool:
    """Buffer a GET for *expr_id*. The flush thread sends it.

    Returns False when there is nowhere to send it (no provider or peer), so
    the caller can give up instead of waiting. True means the GET is buffered,
    or one is already buffered or in flight and the caller should just poll.

    Args:
        contact: ``RelayEndpoint`` to send to instead of the
            routed destination (redirects and hints).
        preclaimed: The pending request was already claimed (redirect retries),
            so the flush must not claim it again.
        block_hash: Ask the provider of this block for *expr_id* (a scoped
            entry) instead of routing by *expr_id*.
        shape: The shape of a DYNAMIC (0) request, or the encoded radix param
            of a RADIX (4) request. Empty for the other resolutions.

    Returns:
        bool: True if the GET was buffered or already exists, False if nowhere to send.

    Raises:
        ValueError: If the shape is malformed or longer than
            ``storage_shape_max_bytes``, the radix param is malformed or its
            key is longer than ``storage_radix_key_max_bytes``, or a shape is
            given with a resolution other than DYNAMIC or RADIX.
    """
    if resolution == RESOLUTION_DYNAMIC:
        parse_shape(shape)
        if len(shape) > node.config.storage_shape_max_bytes:
            raise ValueError(
                f"shape is {len(shape)} bytes, the limit is "
                f"{node.config.storage_shape_max_bytes}"
            )
    elif resolution == RESOLUTION_RADIX:
        _, key = parse_radix_param(shape)
        if len(key) > node.config.storage_radix_key_max_bytes:
            raise ValueError(
                f"radix key is {len(key)} bytes, the limit is "
                f"{node.config.storage_radix_key_max_bytes}"
            )
    elif shape:
        raise ValueError("a shape needs resolution 0 (DYNAMIC) or 4 (RADIX)")
    with node.storage_request_buffer_lock:
        if expr_id in node.storage_request_buffered:
            return True
    if not preclaimed and has_expr_req(node, expr_id):
        return True

    resolved = _resolve_destination(node, expr_id, contact, block_hash)
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
        destination.entries[expr_id] = (resolution, preclaimed, block_hash, shape)
        destination.size += get_entry_size((expr_id, resolution, block_hash, shape))
        node.storage_request_buffered.add(expr_id)
        # Full when one more scoped entry would not fit.
        full = destination.size + SCOPED_GET_ENTRY_BYTES > _body_budget(node)
    if full:
        node.storage_request_flush_event.set()
    return True


def _send_chunk(
    node: "Node", destination: _Destination, chunk: list[tuple]
) -> bool:
    """Send one STORAGE_GET datagram to a destination.

    Args:
        node: A Node instance with outgoing queue.
        destination: The destination to send to.
        chunk: List of (expr_id, resolution, block_hash, shape) tuples.

    Returns:
        True if queued successfully, False otherwise.
    """
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


def _chunks(entries: list[tuple], budget: int) -> list[list[tuple]]:
    """Split entries into chunks that each fit *budget* bytes, in order.

    Args:
        entries: ``(expr_id, resolution, block_hash, shape)`` tuples.
        budget: The entry bytes one datagram holds.

    Returns:
        The chunks. A chunk takes entries until the next one would not fit.
        An entry longer than the budget is a chunk of its own.
    """
    chunks: list[list[tuple]] = []
    used = 0
    for entry in entries:
        size = get_entry_size(entry)
        if not chunks or used + size > budget:
            chunks.append([])
            used = 0
        chunks[-1].append(entry)
        used += size
    return chunks


def flush_storage_requests(node: "Node") -> int:
    """Send everything buffered. Returns the number of datagrams queued.

    Destinations go lowest difficulty first so a busy peer's proof-of-work
    does not hold up the rest. Claims are taken here, right before sending.

    Args:
        node: A Node instance with a request buffer.

    Returns:
        The number of datagrams queued.
    """
    with node.storage_request_buffer_lock:
        pending = node.storage_request_buffer
        node.storage_request_buffer = {}
        node.storage_request_buffered.clear()
    if not pending:
        return 0

    budget = _body_budget(node)
    sent = 0
    for destination in sorted(pending.values(), key=lambda d: d.difficulty):
        live: list[tuple[bytes, int, bytes | None, bytes]] = []
        for expr_id, (resolution, preclaimed, block_hash, shape) in destination.entries.items():
            if preclaimed:
                if not has_expr_req(node, expr_id):
                    continue
            elif not claim_expr_req(node, expr_id, resolution, block_hash, shape):
                continue
            live.append((expr_id, resolution, block_hash, shape))

        for chunk in _chunks(live, budget):
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
                for expr_id, *_ in chunk:
                    pop_expr_req(node, expr_id)
    return sent


def request_storage(node: "Node") -> None:
    """Flush the request buffer on a timer until ``communication_stop_event``.

    Runs as ``request_storage_thread``. A destination filling a datagram sets
    ``storage_request_flush_event`` to flush early.

    Args:
        node: A Node instance with a request buffer and flush event.

    Returns:
        None. Runs indefinitely until ``communication_stop_event`` fires.
    """
    stop = node.communication_stop_event
    event = node.storage_request_flush_event
    interval = float(node.config.storage_request_flush_interval)
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
