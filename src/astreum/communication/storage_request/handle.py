from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import increment_peer_metric
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.communication.storage_request.payment_required import (
    _queue_storage_payment_required,
    _requires_storage_channel,
)
from astreum.communication.storage_request.peer_contact import encode_peer_contact_bytes
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_found import (
    encode_found_pages,
    found_page_expr_bytes,
)
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.expression import (
    RESOLUTION_SINGLE,
    RESOLUTION_LIST,
    RESOLUTION_FULL,
    collect_list,
    collect_full,
)
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.requests import DEFAULT_STORAGE_FOUND_MAX_PAGES
from astreum.storage.records import get_record_from_cold_storage
from astreum.communication.util import xor_distance
from astreum.storage.providers import provider_id_for_payload, provider_payload_for_id

if TYPE_CHECKING:
    from astreum.communication import Node
    from astreum.communication.models.peer import Peer


def _collect_for_resolution(expr, desired: int) -> list:
    """Collect exprs based on desired resolution, sending best available.

    Args:
        expr (Any): The expression to collect from.
        desired (int): The desired resolution level.

    Returns:
        list: A list of collected expressions.
    """
    if desired >= RESOLUTION_FULL and expr._tag == "link":
        return collect_full(expr)
    if desired >= RESOLUTION_LIST and expr._tag == "link":
        return collect_list(expr)
    return [expr]


# Max records fetched inline (in the message-handling thread) per received
# STORAGE_PUT; the rest drain through the long-term store loop.
BATCH_INLINE_FETCH_LIMIT = 8


def _handle_put(
    node: "Node", peer: "Peer", storage_request: StorageRequest
) -> tuple[bool, str | None]:
    """Handle a STORAGE_PUT request by indexing and forwarding entries.

    Args:
        node (Node): The node instance handling the request.
        peer (Peer): The peer sending the request.
        storage_request (StorageRequest): The storage request object.

    Returns:
        tuple[bool, str | None]: A tuple of (success, error_reason).
    """
    from astreum.storage.admission import get_latest_storage_account, is_expr_in_latest_block
    from astreum.storage.exprs.network import destination_peer, send_put_entries
    from astreum.storage.radix import RadixTree, get_from_radix_tree
    from astreum.storage.records import fetch_and_store_record, parse_record

    entries = storage_request.entries or []
    survivors = [
        (expr_id, payload_type)
        for expr_id, payload_type in entries
        if is_expr_in_latest_block(node, expr_id)
    ]
    rejected = len(entries) - len(survivors)
    if rejected:
        node.logger.debug(
            "STORAGE_PUT from %s: rejected %d/%d uncommitted exprs",
            peer.address,
            rejected,
            len(entries),
        )
    if not survivors:
        return False, "STORAGE_PUT rejected: no exprs committed"

    to_self: list[bytes] = []
    by_destination: dict[bytes, tuple[object, list[tuple[bytes, int]]]] = {}
    for expr_id, payload_type in survivors:
        try:
            target = destination_peer(node, expr_id)
        except Exception as exc:
            node.logger.debug(
                "STORAGE_PUT destination lookup failed for %s: %s", expr_id.hex(), exc
            )
            target = None
        if target is None:
            to_self.append(expr_id)
        else:
            by_destination.setdefault(target.public_key_bytes, (target, []))[1].append(
                (expr_id, payload_type)
            )

    if to_self:
        storage_account = get_latest_storage_account(node)
        provider_id = provider_id_for_payload(node, storage_request.data)
        fetch_budget = BATCH_INLINE_FETCH_LIMIT if getattr(node, "long_term_storage", False) else 0
        indexed = 0
        for expr_id in to_self:
            stored_value = None
            if storage_account is not None:
                stored_value = get_from_radix_tree(storage_account.data, node, expr_id)
            record = parse_record(node, stored_value)
            if record is None:
                continue
            node.storage_index[expr_id] = provider_id
            indexed += 1
            if fetch_budget <= 0 or get_record_from_cold_storage(node, expr_id) is not None:
                continue
            fetch_budget -= 1
            try:
                fetch_and_store_record(node, expr_id, record)
            except Exception as exc:
                node.logger.debug("Inline record fetch failed for %s: %s", expr_id.hex(), exc)
        node.logger.debug(
            "STORAGE_PUT from %s: indexed %d/%d self-closest exprs",
            peer.address,
            indexed,
            len(to_self),
        )

    for target, destination_list in by_destination.values():
        node.logger.debug(
            "Forwarding STORAGE_PUT of %d exprs to nearer peer %s",
            len(destination_list),
            target.address,
        )
        for _chunk, ok, reason in send_put_entries(
            node, target, storage_request.data, destination_list
        ):
            if not ok:
                node.logger.debug("STORAGE_PUT forward failed: %s", reason)
    return True, None


def _serve_get(
    node: "Node",
    peer: "Peer",
    expr_id: bytes,
    desired: int,
    block_hash: bytes | None = None,
) -> tuple[bool, str | None]:
    """Serve one STORAGE_GET entry: paged ``STORAGE_FOUND``, a provider redirect, or a payment-required reply.

    A scoped entry (*block_hash* given) is served the same way, by a local get
    of *expr_id*: the block only decides who is asked. A miss is redirected by
    the block's provider or nearest peer instead of by *expr_id*'s.

    Args:
        node (Node): The node instance serving the request.
        peer (Peer): The peer making the request.
        expr_id (bytes): The expression ID to retrieve.
        desired (int): The desired resolution level.
        block_hash (bytes | None): Optional block hash for scoped requests.

    Returns:
        tuple[bool, str | None]: A tuple of (success, error_reason).
    """
    local_atom = get_expr_from_local_storage(node, expr_id)
    if local_atom is not None:
        exprs = _collect_for_resolution(local_atom, desired)
        shared_storage_size = sum(len(encode_expr_to_bytes(e)) for e in exprs)
        if _requires_storage_channel(node, peer, shared_storage_size):
            node.logger.info(
                "Fair-use limit reached for %s while serving %s; channel/payment required",
                peer.address,
                expr_id.hex(),
            )
            _queue_storage_payment_required(
                node,
                peer,
                expr_id,
                shared_storage_size,
            )
            return True, None
        node.logger.debug(
            "Expr %s found locally (resolution=%d, exprs=%d); returning to %s",
            expr_id.hex(),
            desired,
            len(exprs),
            peer.address,
        )
        def _skip(expr, size: int) -> None:
            node.logger.error(
                "STORAGE_FOUND for %s: skipping oversized expr %s (%d bytes)",
                expr_id.hex(),
                expr.hash().hex(),
                size,
            )

        try:
            payloads = encode_found_pages(exprs, on_skip=_skip)
        except ValueError as exc:
            node.logger.error(
                "STORAGE_FOUND for %s not sent: %s", expr_id.hex(), exc
            )
            return False, "root expr too large"
        max_pages = (getattr(node, "config", None) or {}).get(
            "storage_found_max_pages", DEFAULT_STORAGE_FOUND_MAX_PAGES
        )
        if len(payloads) > max_pages:
            node.logger.error(
                "STORAGE_FOUND for %s not sent: %d pages exceeds storage_found_max_pages=%d",
                expr_id.hex(),
                len(payloads),
                max_pages,
            )
            return False, "response too large"
        payload_sizes = [found_page_expr_bytes(p) for p in payloads]
        node.logger.debug(
            "STORAGE_FOUND for %s split into %d pages",
            expr_id.hex(),
            len(payloads),
        )

        for found_payload, payload_size in zip(payloads, payload_sizes):
            resp = StorageResponse(
                code=StorageResponseCode.STORAGE_FOUND,
                data=found_payload,
                expr_id=expr_id,
            )
            resp_msg = Message(
                topic=MessageTopic.STORAGE_RESPONSE,
                body=resp.to_bytes(),
                sender_public_key_bytes=node.storage_public_key_bytes,
            )
            resp_msg.encrypt(peer.shared_key_bytes)
            queued = enqueue_outgoing(
                node,
                peer.address,
                message=resp_msg,
                difficulty=peer.difficulty,
            )
            if queued:
                increment_peer_metric(peer, "shared_storage_upload", payload_size)
        return True, None

    route_id = block_hash if block_hash is not None else expr_id
    if route_id in node.storage_index:
        provider_id = node.storage_index[route_id]
        provider_bytes = provider_payload_for_id(node, provider_id)
        if provider_bytes is not None:
            node.logger.debug("Known provider for %s; informing %s", route_id.hex(), peer.address)
            resp = StorageResponse(
                code=StorageResponseCode.STORAGE_PROVIDER,
                data=provider_bytes,
                expr_id=expr_id,
            )
            resp_msg = Message(
                topic=MessageTopic.STORAGE_RESPONSE,
                body=resp.to_bytes(),
                sender_public_key_bytes=node.storage_public_key_bytes,
            )
            resp_msg.encrypt(peer.shared_key_bytes)
            enqueue_outgoing(
                node,
                peer.address,
                message=resp_msg,
                difficulty=peer.difficulty,
            )
            return True, None
        node.logger.debug(
            "Unknown provider id %s for %s",
            provider_id,
            route_id.hex(),
        )

    nearest_peer = node.peer_route.closest_peer_for_hash(route_id)
    if nearest_peer:
        node.logger.debug("Forwarding requester %s to nearest peer for %s", peer.address, route_id.hex())
        peer_info = encode_peer_contact_bytes(nearest_peer)
        resp = StorageResponse(
            code=StorageResponseCode.STORAGE_PROVIDER,
            data=peer_info,
            expr_id=expr_id,
        )
        resp_msg = Message(
            topic=MessageTopic.STORAGE_RESPONSE,
            body=resp.to_bytes(),
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
        resp_msg.encrypt(peer.shared_key_bytes)
        enqueue_outgoing(
            node,
            peer.address,
            message=resp_msg,
            difficulty=peer.difficulty,
        )
        return True, None

    if route_id in node.storage_index:
        return False, f"unknown provider id {node.storage_index[route_id]} for {route_id.hex()}"
    return True, None


def handle_storage_request(node: "Node", peer: "Peer", message: Message) -> tuple[bool, str | None]:
    """Handle a STORAGE_REQUEST message from a peer.

    Args:
        node (Node): The node instance handling the request.
        peer (Peer): The peer sending the request.
        message (Message): The message containing the storage request.

    Returns:
        tuple[bool, str | None]: A tuple of (success, error_reason).
    """
    if message.content is None:
        node.logger.debug("STORAGE_REQUEST from %s missing content", peer.address)
        return False, "missing content"

    try:
        storage_request = StorageRequest.from_bytes(message.content)
    except Exception as exc:
        node.logger.debug("Error decoding STORAGE_REQUEST from %s: %s", peer.address, exc)
        return False, "decode failed"

    match storage_request.code:
        case StorageRequestCode.STORAGE_GET:
            entries = storage_request.entries or []
            node.logger.debug(
                "Handling STORAGE_GET of %d exprs from %s", len(entries), peer.address
            )
            seen: set[tuple[bytes, bytes | None]] = set()
            served = 0
            failures: list[str] = []
            for expr_id, desired, block_hash in entries:
                if (expr_id, block_hash) in seen:
                    continue
                seen.add((expr_id, block_hash))
                try:
                    ok, reason = _serve_get(node, peer, expr_id, desired, block_hash)
                except Exception as exc:
                    node.logger.debug(
                        "STORAGE_GET entry %s from %s failed: %s",
                        expr_id.hex(),
                        peer.address,
                        exc,
                    )
                    ok, reason = False, f"entry failed: {exc}"
                if ok:
                    served += 1
                else:
                    failures.append(reason or "entry failed")
            if served == 0 and failures:
                return False, failures[0]
            return True, None

        case StorageRequestCode.STORAGE_PUT:
            node.logger.debug(
                "Handling STORAGE_PUT of %d exprs from %s",
                len(storage_request.entries or []),
                peer.address,
            )
            return _handle_put(node, peer, storage_request)

        case _:
            node.logger.debug("Unknown StorageRequestCode %s from %s", storage_request.code, peer.address)
            return False, f"unknown StorageRequestCode {storage_request.code}"

    return True, None
