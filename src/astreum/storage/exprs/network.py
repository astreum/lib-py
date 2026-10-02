from __future__ import annotations

import socket

from time import sleep
from typing import Optional

from astreum.storage.exprs.local import get_expr_from_local_storage
from astreum.expression import Expr, ZERO32, RESOLUTION_SINGLE, RESOLUTION_LIST, RESOLUTION_FULL


def _collect_missing_hashes(expr: Expr, resolution: int) -> list[bytes]:
    """Return unresolved hashes from a partially-resolved expr."""
    missing: list[bytes] = []
    if resolution == RESOLUTION_LIST:
        current = expr
        while current is not None and current._tag == "link":
            if current._tail_hash is not None:
                missing.append(current._tail_hash)
                break
            current = current._tail
    elif resolution == RESOLUTION_FULL:
        stack = [expr]
        while stack:
            e = stack.pop()
            if e._tag != "link":
                continue
            if e._head is not None:
                stack.append(e._head)
            elif e._head_hash is not None:
                missing.append(e._head_hash)
            if e._tail is not None:
                stack.append(e._tail)
            elif e._tail_hash is not None:
                missing.append(e._tail_hash)
    return missing


def _poll_local(node, expr_ids: list[bytes]) -> dict[bytes, Optional[Expr]]:
    """Poll local storage for *expr_ids* together, in the fetch window.

    Waits overlap: one ``storage_fetch_retries x storage_fetch_interval``
    window covers every hash. Hashes that never arrive map to ``None``.
    """
    interval = node.config["storage_fetch_interval"]
    retries = node.config["storage_fetch_retries"]
    found: dict[bytes, Optional[Expr]] = {h: None for h in expr_ids}
    remaining = list(expr_ids)

    def _sweep() -> None:
        for h in list(remaining):
            expr = get_expr_from_local_storage(node, h)
            if expr is not None:
                found[h] = expr
                remaining.remove(h)

    if interval <= 0 or retries <= 0:
        _sweep()
        return found
    for _ in range(retries):
        _sweep()
        if not remaining:
            return found
        sleep(interval)
    _sweep()
    return found


def _resolve_structured(node, expr_id: bytes, resolution: int) -> Optional[Expr]:
    """Poll for a LIST/FULL expr, fetching its missing inner hashes as one batch."""
    interval = node.config["storage_fetch_interval"]
    retries = node.config["storage_fetch_retries"]

    if resolution == RESOLUTION_LIST:
        from astreum.storage.exprs.list import get_expr_list_from_local_storage as read_local
    else:
        from astreum.storage.exprs.full import get_expr_full_from_local_storage as read_local

    for _ in range(max(retries, 1)):
        raw = read_local(node, expr_id)
        if raw is not None:
            missing = _collect_missing_hashes(raw, resolution)
            if not missing:
                return raw
            get_exprs_from_network(node, [(h, RESOLUTION_SINGLE) for h in missing])
        else:
            sleep(interval)
    return read_local(node, expr_id)


def get_exprs_from_network(
    node, entries: list[tuple[bytes, int]]
) -> dict[bytes, Optional[Expr]]:
    """Fetch many Exprs from the P2P network, batching the requests.

    Every entry goes into the request buffer; ``request_storage_thread`` sends
    GETs for the same destination together, one ``STORAGE_GET`` per
    ``max_get_entries`` exprs. Local storage is then polled for all of them at
    once, so the waits overlap.

    For ``RESOLUTION_LIST`` / ``RESOLUTION_FULL`` the fetched root is inspected
    via ``_collect_missing_hashes`` and the unresolved inner hashes are fetched
    together as ``RESOLUTION_SINGLE`` requests.

    Args:
        node: A Node instance providing config and storage access.
        entries: ``(expr_id, resolution)`` pairs. A repeated ``expr_id`` is
            fetched once, at its first resolution.

    Returns:
        ``{expr_id: Expr | None}`` for every requested hash. ``None`` means the
        fetch timed out, nothing could be sent (no provider or peer), or the
        node is disconnected. A ``RESOLUTION_SINGLE`` hash that is already
        local is returned without a request.
    """
    requested: dict[bytes, int] = {}
    for expr_id, resolution in entries:
        requested.setdefault(expr_id, resolution)
    results: dict[bytes, Optional[Expr]] = {h: None for h in requested}

    if not node.is_connected:
        node.logger.debug("Network fetch skipped for %d exprs; node not connected", len(requested))
        return results

    from astreum.storage.workers.requests import queue_storage_get

    waiting_flat: list[bytes] = []
    waiting_structured: list[tuple[bytes, int]] = []
    for expr_id, resolution in requested.items():
        if resolution == RESOLUTION_SINGLE:
            local = get_expr_from_local_storage(node, expr_id)
            if local is not None:
                results[expr_id] = local
                continue
        node.logger.debug("Attempting network fetch for %s (resolution=%s)", expr_id.hex(), resolution)
        if not queue_storage_get(node, expr_id, resolution):
            node.logger.debug("Network request failed for %s: nothing to send to", expr_id.hex())
            continue
        if resolution in (RESOLUTION_LIST, RESOLUTION_FULL):
            waiting_structured.append((expr_id, resolution))
        else:
            waiting_flat.append(expr_id)

    if waiting_flat:
        results.update(_poll_local(node, waiting_flat))
    for expr_id, resolution in waiting_structured:
        results[expr_id] = _resolve_structured(node, expr_id, resolution)
    return results


def get_expr_from_network(node, expr_id: bytes, resolution: int = RESOLUTION_SINGLE) -> Optional[Expr]:
    """Fetch one Expr from the P2P network; a batch of one.

    See :func:`get_exprs_from_network`.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to fetch.
        resolution: The resolution strategy — ``RESOLUTION_SINGLE``
            (default), ``RESOLUTION_LIST``, ``RESOLUTION_FULL`` or
            ``RESOLUTION_RECORD``.

    Returns:
        The fetched Expr, or None if all retries are exhausted, nothing could
        be sent, or the node is disconnected.
    """
    return get_exprs_from_network(node, [(expr_id, resolution)]).get(expr_id)


def _is_resolved_locally(node, expr_id: bytes, resolution: int) -> bool:
    """True if nothing is left to fetch for *expr_id* at *resolution*."""
    if resolution == RESOLUTION_SINGLE:
        return get_expr_from_local_storage(node, expr_id) is not None
    if resolution == RESOLUTION_LIST:
        from astreum.storage.exprs.list import get_expr_list_from_local_storage as read_local
    elif resolution == RESOLUTION_FULL:
        from astreum.storage.exprs.full import get_expr_full_from_local_storage as read_local
    else:
        return False
    raw = read_local(node, expr_id)
    return raw is not None and not _collect_missing_hashes(raw, resolution)


def prefetch_exprs_from_network(
    node, expr_ids, resolution: int = RESOLUTION_SINGLE
) -> None:
    """Fetch, as one batch, the *expr_ids* that are not already fully local.

    For callers that know a whole hash list up front and would otherwise fetch
    it one hash at a time: call this first, and the existing per-item code then
    finds everything in local storage. It is an optimisation only, so it never
    raises; whatever is still missing afterwards is fetched by the caller's
    normal path.
    """
    try:
        if not getattr(node, "is_connected", False):
            return
        wanted = [
            (h, resolution)
            for h in dict.fromkeys(expr_ids)
            if h and h != ZERO32 and not _is_resolved_locally(node, h, resolution)
        ]
        if wanted:
            get_exprs_from_network(node, wanted)
    except Exception as exc:
        node.logger.debug("Prefetch of %d exprs failed: %s", len(list(expr_ids)), exc)


def build_provider_payload(node) -> bytes:
    """Encode this node's provider contact (70 bytes):
    storage key + relay key + ipv4 + port.  Raises on missing/invalid config."""
    provider_ip_bytes = socket.inet_aton(node.relay_ip_address)
    provider_port_bytes = int(node.config["port"]).to_bytes(2, "big", signed=False)
    return (
        node.config["storage_public_key_bytes"]
        + node.config["relay_public_key_bytes"]
        + provider_ip_bytes
        + provider_port_bytes
    )


def destination_peer(node, expr_id: bytes):
    """Return the peer *expr_id*'s ad should be sent to, or ``None`` when this
    node is itself the closest (same xor comparison used for all STORAGE_PUT routing)."""
    from astreum.communication.util import xor_distance

    closest_peer = node.peer_route.closest_peer_for_hash(expr_id)
    if closest_peer is None or closest_peer.address is None:
        return None
    try:
        self_distance = xor_distance(expr_id, node.config["storage_public_key_bytes"])
        peer_distance = xor_distance(expr_id, closest_peer.public_key_bytes)
    except Exception as exc:
        node.logger.debug("Failed computing distance for expr %s: %s", expr_id.hex(), exc)
        return None
    if self_distance <= peer_distance:
        return None
    return closest_peer


def _send_storage_put(node, peer, storage_req) -> tuple[bool, str | None]:
    """Encrypt and enqueue one StorageRequest to *peer* at the peer's difficulty."""
    from astreum.communication.models.message import Message, MessageTopic
    from astreum.communication.outgoing_queue import enqueue_outgoing

    try:
        message = Message(
            topic=MessageTopic.STORAGE_REQUEST,
            content=storage_req.to_bytes(),
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
        message.encrypt(peer.shared_key_bytes)
        queued = enqueue_outgoing(
            node,
            peer.address,
            message=message,
            difficulty=peer.difficulty,
        )
    except Exception as exc:
        node.logger.debug("Failed to queue storage put to %s: %s", peer.address, exc)
        return False, f"failed to queue advertisement: {exc}"
    if not queued:
        return False, "enqueue_outgoing dropped advertisement"
    return True, None


def send_put_entries(
    node,
    peer,
    provider_payload: bytes,
    entries: list[tuple[bytes, int]],
) -> list[tuple[list[tuple[bytes, int]], bool, str | None]]:
    """Send *entries* (``(expr_id, payload_type)``) to *peer*, chunked so every
    ``STORAGE_PUT`` datagram fits ``storage_put_batch_max_bytes``.  Returns one
    ``(chunk_entries, ok, reason)`` tuple per datagram sent.
    """
    from astreum.communication.storage_request.code import StorageRequestCode
    from astreum.communication.storage_request.model import (
        StorageRequest,
        max_batch_entries,
    )
    from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES

    budget = node.config.get("storage_put_batch_max_bytes", DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES)
    per_batch = max(1, max_batch_entries(budget))

    results = []
    for start in range(0, len(entries), per_batch):
        chunk = entries[start : start + per_batch]
        req = StorageRequest(
            code=StorageRequestCode.STORAGE_PUT,
            data=provider_payload,
            entries=chunk,
        )
        ok, reason = _send_storage_put(node, peer, req)
        results.append((chunk, ok, reason))
    return results


def put_exprs_in_network(
    node, entries: list[tuple[bytes, int]]
) -> list[tuple[bytes, bool, str | None]]:
    """Advertise many exprs, bucketing them by destination peer.

    Exprs whose closest node is this node are self-indexed.  Every other
    destination's list goes out through :func:`send_put_entries` (one PoW per
    datagram).  Returns ``(expr_id, ok, reason)`` per input entry.
    """
    from astreum.storage.providers import provider_id_for_payload

    try:
        provider_payload = build_provider_payload(node)
    except Exception as exc:
        reason = f"unable to encode provider info: {exc}"
        return [(expr_id, False, reason) for expr_id, _ in entries]

    results: list[tuple[bytes, bool, str | None]] = []
    by_destination: dict[bytes, tuple[object, list[tuple[bytes, int]]]] = {}
    for expr_id, payload_type in entries:
        try:
            peer = destination_peer(node, expr_id)
        except Exception as exc:
            results.append((expr_id, False, f"peer lookup failed: {exc}"))
            continue
        if peer is None:
            node.storage_index[expr_id] = provider_id_for_payload(node, provider_payload)
            results.append((expr_id, True, None))
            continue
        by_destination.setdefault(peer.public_key_bytes, (peer, []))[1].append(
            (expr_id, payload_type)
        )

    for peer, destination_list in by_destination.values():
        for chunk, ok, reason in send_put_entries(node, peer, provider_payload, destination_list):
            for expr_id, _ in chunk:
                results.append((expr_id, ok, reason))
    return results
