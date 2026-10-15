from __future__ import annotations

import socket

from time import sleep
from typing import Optional

from astreum.storage.exprs.local import get_expr_from_local_storage
from astreum.expression import Expr, ZERO32, RESOLUTION_SINGLE, RESOLUTION_LIST, RESOLUTION_FULL
from astreum.expression.helpers import get_expr_builtin_tag


def _is_builtin_type_hash(h: bytes) -> bool:
    """The type symbol of an int, str or float: known, never fetched.

    Args:
        h: The 32-byte content hash to check.

    Returns:
        True if h is a builtin type hash, False otherwise.
    """
    from astreum.expression.expr import _BUILTIN_TYPE_HASH, BUILTIN_COMPOSITE_TYPE_NAMES

    return _BUILTIN_TYPE_HASH.get(h) in BUILTIN_COMPOSITE_TYPE_NAMES


def _collect_missing_hashes(expr: Expr, resolution: int) -> list[bytes]:
    """Return unresolved hashes from a partially-resolved expr.

    Args:
        expr: The expression to inspect.
        resolution: The resolution strategy (LIST or FULL).

    Returns:
        List of unresolved 32-byte content hashes.
    """
    missing: list[bytes] = []
    if resolution == RESOLUTION_LIST:
        current = expr
        while current is not None and get_expr_builtin_tag(current) == "link":
            if current.tail_hash is not None:
                missing.append(current.tail_hash)
                break
            current = current.tail
    elif resolution == RESOLUTION_FULL:
        stack = [expr]
        while stack:
            e = stack.pop()
            if get_expr_builtin_tag(e) != "link":
                continue
            if e.head is not None:
                stack.append(e.head)
            elif e.head_hash is not None:
                missing.append(e.head_hash)
            if e.tail is not None:
                stack.append(e.tail)
            elif e.tail_hash is not None and not _is_builtin_type_hash(e.tail_hash):
                missing.append(e.tail_hash)
    return missing


def _poll_local(node, expr_ids: list[bytes]) -> dict[bytes, Optional[Expr]]:
    """Poll local storage for *expr_ids* together, in the fetch window.

    Waits overlap: one ``storage_fetch_retries x storage_fetch_interval``
    window covers every hash. Hashes that never arrive map to ``None``.

    Args:
        node: A Node instance for storage access.
        expr_ids: List of 32-byte content hashes to poll for.

    Returns:
        Dict mapping each expr_id to its Expr (or None if not found).
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


def _resolve_structured(
    node, expr_id: bytes, resolution: int, contact=None, block_hash=None
) -> Optional[Expr]:
    """Poll for a LIST/FULL expr, fetching its missing inner hashes as one batch.

    *contact* and *block_hash* keep the follow-up fetch of the missing inner
    hashes on the same peer and block as the root fetch: a cell reached by
    plain hash is in the block of the cell that holds it.

    Args:
        node: A Node instance for storage access.
        expr_id: The content hash of the root expression.
        resolution: LIST or FULL resolution strategy.
        contact: Optional contact for network fetches.
        block_hash: Optional block hash for scoped fetches.

    Returns:
        The fully resolved Expr, or None.
    """
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
            get_exprs_from_network(
                node,
                [(h, RESOLUTION_SINGLE) for h in missing],
                contact=contact,
                **({} if block_hash is None else {"block_hash": block_hash}),
            )
        else:
            sleep(interval)
    return read_local(node, expr_id)


def get_exprs_from_network(
    node, entries: list[tuple[bytes, int]], contact=None, block_hash=None
) -> dict[bytes, Optional[Expr]]:
    """Fetch many Exprs from the P2P network, batching the requests.

    Every entry goes into the request buffer. ``request_storage_thread`` sends
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
        contact: ``(relay_key, address, port)`` of the one peer to ask, instead
            of routing each hash by the index or xor distance. Used for data
            only its creator holds (an uncommitted block's exprs). Local
            storage is still checked first.
        block_hash: Ask the provider of this block for every entry (scoped
            GETs, routed by the block hash) instead of routing each by its own
            hash. Only a miss in local storage is affected.

    Returns:
        ``{expr_id: Expr | None}`` for every requested hash. ``None`` means the
        fetch timed out, nothing could be sent (no provider or peer), or the
        node is disconnected. Return local for a ``RESOLUTION_SINGLE`` hash already
        present without a request.
    """
    requested: dict[bytes, int] = {}
    for expr_id, resolution in entries:
        requested.setdefault(expr_id, resolution)
    results: dict[bytes, Optional[Expr]] = {h: None for h in requested}

    if not node.is_connected:
        node.logger.debug("Network fetch skipped for %d exprs; node not connected", len(requested))
        return results

    from astreum.storage.workers.requests import queue_storage_get

    # Only a scoped call passes block_hash on, so a GET by expr_id alone is
    # queued exactly as before. A *contact* already names the peer to ask,
    # which makes the block redundant.
    scope = {} if block_hash is None or contact is not None else {"block_hash": block_hash}
    waiting_flat: list[bytes] = []
    waiting_structured: list[tuple[bytes, int]] = []
    for expr_id, resolution in requested.items():
        if resolution == RESOLUTION_SINGLE:
            local = get_expr_from_local_storage(node, expr_id)
            if local is not None:
                results[expr_id] = local
                continue
        node.logger.debug("Attempting network fetch for %s (resolution=%s)", expr_id.hex(), resolution)
        if not queue_storage_get(node, expr_id, resolution, contact=contact, **scope):
            node.logger.debug("Network request failed for %s: nothing to send to", expr_id.hex())
            continue
        if resolution in (RESOLUTION_LIST, RESOLUTION_FULL):
            waiting_structured.append((expr_id, resolution))
        else:
            waiting_flat.append(expr_id)

    if waiting_flat:
        results.update(_poll_local(node, waiting_flat))
    for expr_id, resolution in waiting_structured:
        results[expr_id] = _resolve_structured(
            node, expr_id, resolution, contact, block_hash
        )

    # The first provider did not answer. Ask the second provider once. A
    # caller that named a contact asked one peer on purpose.
    if contact is None:
        missing = [
            (h, requested[h])
            for h in (*waiting_flat, *(h for h, _ in waiting_structured))
            if results[h] is None
        ]
        resent = _resend_to_next_provider(node, missing, block_hash)
        flat = [h for h in waiting_flat if h in resent]
        if flat:
            results.update(_poll_local(node, flat))
        for expr_id, resolution in waiting_structured:
            if expr_id in resent:
                results[expr_id] = _resolve_structured(
                    node, expr_id, resolution, contact, block_hash
                )
    return results


def _resend_to_next_provider(
    node, missing: list[tuple[bytes, int]], block_hash=None
) -> set[bytes]:
    """Queue the GET of each missing hash to its second provider.

    The second contact comes from the pending request, where the
    STORAGE_PROVIDER reply handler saved it, or from the second provider in the
    index of this node. A hash with no second provider is skipped.

    Args:
        node: A Node instance with the request registry and the index.
        missing: ``(expr_id, resolution)`` pairs that the first poll missed.
        block_hash: Optional block hash of a scoped fetch.

    Returns:
        The set of hashes for which a GET was queued.
    """
    from astreum.communication.storage_response.storage_provider import (
        decode_storage_provider,
    )
    from astreum.storage.providers import provider_payload_for_id
    from astreum.storage.requests import has_expr_req, rearm_expr_req, take_next_contact
    from astreum.storage.workers.requests import queue_storage_get

    scope = {} if block_hash is None else {"block_hash": block_hash}
    route_key = block_hash
    queued: set[bytes] = set()
    for expr_id, resolution in missing:
        contact = take_next_contact(node, expr_id)
        if contact is None:
            provider_ids = node.storage_index.get(route_key or expr_id, ())
            payload = (
                provider_payload_for_id(node, provider_ids[1]) if len(provider_ids) > 1 else None
            )
            if payload is None:
                continue
            try:
                _, relay_key, address, port = decode_storage_provider(payload)
            except ValueError:
                continue
            contact = (relay_key, address, port)
        live = has_expr_req(node, expr_id)
        if live:
            rearm_expr_req(node, expr_id)
        if queue_storage_get(node, expr_id, resolution, contact=contact, preclaimed=live, **scope):
            queued.add(expr_id)
    return queued


def get_expr_from_network(
    node, expr_id: bytes, resolution: int = RESOLUTION_SINGLE, contact=None, block_hash=None
) -> Optional[Expr]:
    """Fetch one Expr from the P2P network. A batch of one.

    See :func:`get_exprs_from_network`.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to fetch.
        contact: Optional ``(relay_key, address, port)`` to ask directly.
        block_hash: Optional block whose provider holds *expr_id*.
        resolution: The resolution strategy — ``RESOLUTION_SINGLE``
            (default), ``RESOLUTION_LIST`` or ``RESOLUTION_FULL``.

    Returns:
        The fetched Expr, or None if all retries are exhausted, nothing could
        be sent, or the node is disconnected.
    """
    scope = {} if block_hash is None else {"block_hash": block_hash}
    return get_exprs_from_network(
        node, [(expr_id, resolution)], contact=contact, **scope
    ).get(expr_id)


def _is_resolved_locally(node, expr_id: bytes, resolution: int) -> bool:
    """True if nothing is left to fetch for *expr_id* at *resolution*.

    Args:
        node: A Node instance for storage access.
        expr_id: The 32-byte content hash to check.
        resolution: The resolution strategy.

    Returns:
        True if fully resolved locally, False otherwise.
    """
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
    node, expr_ids, resolution: int = RESOLUTION_SINGLE, contact=None, block_hash=None
) -> None:
    """Fetch, as one batch, the *expr_ids* that are not already fully local.

    For callers that know a whole hash list up front and would otherwise fetch
    it one hash at a time: call this first, and the existing per-item code then
    finds everything in local storage. It is an optimisation only, so it never
    raises. Whatever is still missing afterwards is fetched by the caller's
    normal path.

    Args:
        node: A Node instance for network access.
        expr_ids: Iterable of 32-byte content hashes.
        resolution: Resolution strategy (default SINGLE).
        contact: Optional contact for network fetches.
        block_hash: Optional block hash for scoped fetches.

    Returns: None
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
            scope = {} if block_hash is None else {"block_hash": block_hash}
            get_exprs_from_network(node, wanted, contact=contact, **scope)
    except Exception as exc:
        node.logger.debug("Prefetch of %d exprs failed: %s", len(list(expr_ids)), exc)


def build_provider_payload(node) -> bytes:
    """Encode this node's provider contact as a 70-byte payload.

    The payload contains storage key, relay key, ipv4, and port.
    Raises on missing/invalid config.

    Args:
        node: A Node instance with provider network config.

    Returns:
        70-byte encoded provider payload.

    Raises:
        OSError: If the IP address is invalid.
    """
    provider_ip_bytes = socket.inet_aton(node.relay_ip_address)
    provider_port_bytes = int(node.config["port"]).to_bytes(2, "big", signed=False)
    return (
        node.config["storage_public_key_bytes"]
        + node.config["relay_public_key_bytes"]
        + provider_ip_bytes
        + provider_port_bytes
    )


def destination_peer(node, expr_id: bytes):
    """Return the peer *expr_id*'s ad should be sent to, or ``None``.

    Returns ``None`` when this node is itself the closest (same xor
    comparison used for all STORAGE_PUT routing).

    Args:
        node: A Node instance with peer routing.
        expr_id: The 32-byte content hash to route.

    Returns:
        The closest Peer object, or None if this node is closest.
    """
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
    """Encrypt and enqueue one StorageRequest to *peer* at the peer's difficulty.

    Args:
        node: A Node instance with outgoing queue.
        peer: The target Peer.
        storage_req: The StorageRequest to send.

    Returns:
        Tuple of (success, error_reason).
    """
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
    """Send *entries* to *peer*, chunked to fit the byte limit.

    Each entry is ``(expr_id, payload_type)``. Chunks fit
    ``storage_put_batch_max_bytes``. Returns one ``(chunk_entries, ok, reason)``
    tuple per datagram sent.

    Args:
        node: A Node instance with outgoing queue.
        peer: The target Peer.
        provider_payload: One or two joined 70-byte provider payloads.
        entries: List of (expr_id, payload_type) tuples.

    Returns:
        List of (chunk, success, reason) tuples.
    """
    from astreum.communication.storage_request.code import StorageRequestCode
    from astreum.communication.storage_request.model import (
        StorageRequest,
        max_batch_entries,
    )
    from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES

    budget = node.config.get("storage_put_batch_max_bytes", DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES)
    per_batch = max(1, max_batch_entries(budget, len(provider_payload) // 70))

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

    Args:
        node: A Node instance with storage and network access.
        entries: List of (expr_id, payload_type) tuples.

    Returns:
        List of (expr_id, success, reason) tuples.
    """
    from astreum.storage.providers import index_provider, provider_id_for_payload

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
            index_provider(node, expr_id, provider_id_for_payload(node, provider_payload))
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
