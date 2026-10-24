from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import increment_peer_metric
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.models.peer_info import PEER_INFO_BYTES, PeerInfo, encode_peer_info
from astreum.communication.storage_request.model import StorageRequest, parse_radix_param
from astreum.communication.storage_request.payment_required import (
    _queue_storage_payment_required,
    _requires_storage_channel,
)
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_found import (
    encode_found_pages,
    found_page_expr_bytes,
)
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.expression import (
    RESOLUTION_DYNAMIC,
    RESOLUTION_RADIX,
    RESOLUTION_SINGLE,
    RESOLUTION_LIST,
    RESOLUTION_FULL,
    collect_list,
    collect_full,
)
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.shape import parse_shape, walk_shape
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.radix.tree.walk import walk_radix
from astreum.storage.records import get_record_from_cold_storage
from astreum.communication.util import xor_distance
from astreum.consensus.constants import PROVIDERS_PER_RECORD
from astreum.storage.providers import (
    index_provider,
    provider_id_for_payload,
    provider_payload_for_id,
)
from astreum.expression.helpers import get_expr_builtin_tag

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
    if desired >= RESOLUTION_FULL and get_expr_builtin_tag(expr) == "link":
        return collect_full(expr)
    if desired >= RESOLUTION_LIST and get_expr_builtin_tag(expr) == "link":
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
    own_key = node.config.storage_public_key_bytes
    for expr_id, payload_type in survivors:
        try:
            target = destination_peer(node, expr_id)
            nearer = (
                0
                if target is None
                else node.peer_route.count_nearer(expr_id, own_key, PROVIDERS_PER_RECORD)
            )
        except Exception as exc:
            node.logger.debug(
                "STORAGE_PUT destination lookup failed for %s: %s", expr_id.hex(), exc
            )
            target = None
            nearer = 0
        # Index when no peer is nearer, or when fewer than PROVIDERS_PER_RECORD
        # known nodes are nearer (this node is one of the nearest providers).
        if target is None or nearer < PROVIDERS_PER_RECORD:
            to_self.append(expr_id)
        # Forward to a strictly nearer peer, but never back to the sender.
        if target is not None and target.public_key_bytes != peer.public_key_bytes:
            by_destination.setdefault(target.public_key_bytes, (target, []))[1].append(
                (expr_id, payload_type)
            )

    if to_self:
        storage_account = get_latest_storage_account(node)
        data = storage_request.data
        provider_ids = [
            provider_id_for_payload(node, data[i : i + PEER_INFO_BYTES])
            for i in range(0, len(data), PEER_INFO_BYTES)
        ]
        fetch_budget = BATCH_INLINE_FETCH_LIMIT if getattr(node, "is_storage_provider", False) else 0
        indexed = 0
        for expr_id in to_self:
            stored_value = None
            if storage_account is not None:
                stored_value = get_from_radix_tree(storage_account.data, node, expr_id)
            record = parse_record(node, stored_value)
            if record is None:
                continue
            for provider_id in provider_ids:
                index_provider(node, expr_id, provider_id)
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
    shape: bytes = b"",
) -> tuple[bool, str | None]:
    """Serve one STORAGE_GET entry.

    A scoped entry (*block_hash* given) is served the same way, by a local get
    of *expr_id*: the block only decides who is asked. A miss is redirected by
    the block's provider or nearest peer instead of by *expr_id*'s.

    A DYNAMIC entry (resolution 0) is served by walking *shape* over local
    storage. The reply holds the exprs the walk finds. A child this node does
    not hold is left out, and the requester asks for it again.

    A RADIX entry (resolution 4) is served by walking the radix tree from
    *expr_id* towards the key in *shape*, over local storage. The reply holds
    the exprs for each node on the way. The walk stops at the first node this
    node does not hold, and the requester asks again from there.

    Args:
        node (Node): The node instance serving the request.
        peer (Peer): The peer making the request.
        expr_id (bytes): The expression ID to retrieve.
        desired (int): The desired resolution level.
        block_hash (bytes | None): Optional block hash for scoped requests.
        shape (bytes): The shape of a DYNAMIC request, or the radix param of a
            RADIX request, else empty.

    Returns:
        tuple[bool, str | None]: A tuple of (success, error_reason).
    """
    rule = None
    radix = None
    if desired == RESOLUTION_RADIX:
        try:
            radix = parse_radix_param(shape)
        except ValueError as exc:
            return False, f"invalid radix param: {exc}"
        if len(radix[1]) > node.config.storage_radix_key_max_bytes:
            return False, "radix key too long"
    if desired == RESOLUTION_DYNAMIC:
        if len(shape) > node.config.storage_shape_max_bytes:
            return False, "shape too long"
        try:
            rule = parse_shape(shape)
        except ValueError as exc:
            return False, f"invalid shape: {exc}"

    local_atom = get_expr_from_local_storage(node, expr_id)
    if local_atom is not None:
        if radix is not None:
            exprs = walk_radix(node, expr_id, radix[1], radix[0])
            if not exprs:
                return False, "radix selects nothing"
        elif rule is None:
            exprs = _collect_for_resolution(local_atom, desired)
        else:
            exprs, _ = walk_shape(
                rule, expr_id, lambda h: get_expr_from_local_storage(node, h)
            )
            if not exprs:
                return False, "shape selects nothing"
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
        max_pages = node.config.storage_found_max_pages
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
        provider_ids = node.storage_index[route_id]
        provider_bytes = b"".join(
            payload
            for payload in (provider_payload_for_id(node, i) for i in provider_ids)
            if payload is not None
        )
        if provider_bytes:
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
            "Unknown provider ids %s for %s",
            provider_ids,
            route_id.hex(),
        )

    nearest_peer = node.peer_route.closest_peer_for_hash(route_id)
    if nearest_peer:
        node.logger.debug("Forwarding requester %s to nearest peer for %s", peer.address, route_id.hex())
        peer_info = encode_peer_info(
            PeerInfo(
                nearest_peer.public_key_bytes,
                nearest_peer.relay_public_key_bytes,
                nearest_peer.address,
            ),
            False,
        )
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
        return False, f"unknown provider ids {node.storage_index[route_id]} for {route_id.hex()}"
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
            for expr_id, desired, block_hash, shape in entries:
                if (expr_id, block_hash) in seen:
                    continue
                seen.add((expr_id, block_hash))
                try:
                    ok, reason = _serve_get(
                        node, peer, expr_id, desired, block_hash, shape
                    )
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

        case StorageRequestCode.STORAGE_INDEX_REQUEST:
            node.logger.debug("Queued STORAGE_INDEX_REQUEST from %s", peer.address)
            with node.index_requests_lock:
                if peer.public_key_bytes not in node.index_requests:
                    node.index_requests.append(peer.public_key_bytes)
            return True, None

        case _:
            node.logger.debug("Unknown StorageRequestCode %s from %s", storage_request.code, peer.address)
            return False, f"unknown StorageRequestCode {storage_request.code}"

    return True, None
