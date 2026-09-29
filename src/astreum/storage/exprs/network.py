from __future__ import annotations

import socket

from time import sleep
from typing import Optional

from astreum.storage.exprs.local import get_expr_from_local_storage
from astreum.storage.requests import claim_expr_req, pop_expr_req
from astreum.expression import Expr, RESOLUTION_SINGLE, RESOLUTION_LIST, RESOLUTION_FULL


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


def _send_storage_request(node, expr_id: bytes, resolution: int) -> Optional[str]:
    """Send a STORAGE_GET request to a peer. Returns error string or None."""
    from astreum.communication.storage_request.code import StorageRequestCode
    from astreum.communication.storage_request.model import StorageRequest
    from astreum.communication.models.message import Message, MessageTopic
    from astreum.communication.outgoing_queue import enqueue_outgoing

    provider_id = node.storage_index.get(expr_id)
    if provider_id is not None:
        from astreum.storage.providers import provider_payload_for_id
        from astreum.communication.storage_response.storage_provider import decode_storage_provider
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

        provider_payload = provider_payload_for_id(node, provider_id)
        if provider_payload is not None:
            try:
                storage_key_bytes, relay_key_bytes, provider_address, provider_port = decode_storage_provider(provider_payload)
                provider_public_key = X25519PublicKey.from_public_bytes(relay_key_bytes)
                shared_key_bytes = node.relay_secret_key.exchange(provider_public_key)

                storage_req = StorageRequest(
                    code=StorageRequestCode.STORAGE_GET,
                    data=b"",
                    expr_id=expr_id,
                    payload_type=resolution,
                )
                message = Message(
                    topic=MessageTopic.STORAGE_REQUEST,
                    content=storage_req.to_bytes(),
                    sender_public_key_bytes=node.storage_public_key_bytes,
                )
                message.encrypt(shared_key_bytes)
                if not claim_expr_req(node, expr_id, resolution):
                    node.logger.debug("GET for %s already pending", expr_id.hex())
                    return None
                try:
                    queued = enqueue_outgoing(
                        node,
                        (provider_address, provider_port),
                        message=message,
                        difficulty=1,
                    )
                except Exception:
                    pop_expr_req(node, expr_id)
                    raise
                if not queued:
                    pop_expr_req(node, expr_id)
                if queued:
                    node.logger.debug(
                        "Requested %s %s from indexed provider %s:%s",
                        resolution,
                        expr_id.hex(),
                        provider_address,
                        provider_port,
                    )
                else:
                    node.logger.debug(
                        "Dropped request for %s %s to indexed provider %s:%s",
                        resolution,
                        expr_id.hex(),
                        provider_address,
                        provider_port,
                    )
                return None
            except Exception as exc:
                node.logger.debug("Failed indexed fetch for %s: %s", expr_id.hex(), exc)
                return f"failed indexed fetch: {exc}"
        return f"unknown provider id {provider_id}"

    try:
        closest_peer = node.peer_route.closest_peer_for_hash(expr_id)
    except Exception as exc:
        return f"peer lookup failed: {exc}"

    if closest_peer is None or closest_peer.address is None:
        return "no peer available"

    storage_req = StorageRequest(
        code=StorageRequestCode.STORAGE_GET,
        data=b"",
        expr_id=expr_id,
        payload_type=resolution,
    )
    try:
        message = Message(
            topic=MessageTopic.STORAGE_REQUEST,
            content=storage_req.to_bytes(),
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
    except Exception as exc:
        return f"failed to build storage request: {exc}"

    message.encrypt(closest_peer.shared_key_bytes)
    if not claim_expr_req(node, expr_id, resolution):
        node.logger.debug("GET for %s already pending", expr_id.hex())
        return None

    try:
        queued = enqueue_outgoing(
            node,
            closest_peer.address,
            message=message,
            difficulty=closest_peer.difficulty,
        )
        if not queued:
            pop_expr_req(node, expr_id)
        if queued:
            node.logger.debug(
                "Queued STORAGE_GET %s for %s to peer %s",
                resolution,
                expr_id.hex(),
                closest_peer.address,
            )
        else:
            node.logger.debug(
                "Dropped STORAGE_GET %s for %s to peer %s",
                resolution,
                expr_id.hex(),
                closest_peer.address,
            )
    except Exception as exc:
        pop_expr_req(node, expr_id)
        return f"failed to queue STORAGE_GET: {exc}"
    return None


def get_expr_from_network(node, expr_id: bytes, resolution: int = RESOLUTION_SINGLE) -> Optional[Expr]:
    """Fetch an Expr from the P2P network with polling.

    Sends a ``STORAGE_GET`` request to an indexed provider or closest
    peer, then polls local storage with retries until the response
    arrives.  For ``RESOLUTION_LIST`` / ``RESOLUTION_FULL``, the
    response is inspected via ``_collect_missing_hashes`` and any
    unresolved inner hashes are fetched recursively as
    ``RESOLUTION_SINGLE`` requests.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to fetch.
        resolution: The resolution strategy — ``RESOLUTION_SINGLE``
            (default), ``RESOLUTION_LIST``, or ``RESOLUTION_FULL``.

    Returns:
        The fetched Expr, or None if all retries are exhausted or the
        node is disconnected.
    """
    if not node.is_connected:
        node.logger.debug("Network fetch skipped for %s; node not connected", expr_id.hex())
        return None

    node.logger.debug("Attempting network fetch for %s (resolution=%s)", expr_id.hex(), resolution)

    err = _send_storage_request(node, expr_id, resolution)
    if err is not None:
        node.logger.debug("Network request failed for %s: %s", expr_id.hex(), err)
        return None

    interval = node.config["storage_fetch_interval"]
    retries = node.config["storage_fetch_retries"]

    def _poll_single() -> Optional[Expr]:
        if interval <= 0 or retries <= 0:
            return get_expr_from_local_storage(node, expr_id)
        for _ in range(retries):
            expr = get_expr_from_local_storage(node, expr_id)
            if expr is not None:
                return expr
            sleep(interval)
        return get_expr_from_local_storage(node, expr_id)

    def _poll_list() -> Optional[Expr]:
        from astreum.storage.exprs.list import get_expr_list_from_local_storage

        for attempt in range(max(retries, 1)):
            raw = get_expr_list_from_local_storage(node, expr_id)
            if raw is not None:
                missing = _collect_missing_hashes(raw, RESOLUTION_LIST)
                if not missing:
                    return raw
                for h in missing:
                    get_expr_from_network(node, h, RESOLUTION_SINGLE)
            else:
                sleep(interval)
        return get_expr_list_from_local_storage(node, expr_id)

    def _poll_full() -> Optional[Expr]:
        from astreum.storage.exprs.full import get_expr_full_from_local_storage

        for attempt in range(max(retries, 1)):
            raw = get_expr_full_from_local_storage(node, expr_id)
            if raw is not None:
                missing = _collect_missing_hashes(raw, RESOLUTION_FULL)
                if not missing:
                    return raw
                for h in missing:
                    get_expr_from_network(node, h, RESOLUTION_SINGLE)
            else:
                sleep(interval)
        return get_expr_full_from_local_storage(node, expr_id)

    if resolution == RESOLUTION_LIST:
        return _poll_list()
    if resolution == RESOLUTION_FULL:
        return _poll_full()
    return _poll_single()


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
