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
from astreum.communication.storage_response.storage_found import encode_payload
from astreum.communication.outgoing_queue import enqueue_outgoing
from astreum.expression import (
    RESOLUTION_SINGLE,
    RESOLUTION_LIST,
    RESOLUTION_FULL,
    RESOLUTION_RECORD,
    ZERO32,
    collect_list,
    collect_full,
)
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.records import get_record_from_cold_storage
from astreum.communication.util import xor_distance
from astreum.storage.providers import provider_id_for_payload, provider_payload_for_id

if TYPE_CHECKING:
    from astreum.communication import Node
    from astreum.communication.models.peer import Peer


def _collect_for_resolution(expr, desired: int) -> list:
    """Collect exprs based on desired resolution, sending best available."""
    if desired >= RESOLUTION_FULL and expr._tag == "link":
        return collect_full(expr)
    if desired >= RESOLUTION_LIST and expr._tag == "link":
        return collect_list(expr)
    return [expr]


def _collect_record_exprs(node: "Node", root, storage_id: bytes) -> list | None:
    """Assemble a record response: root plus locally-held slot data exprs.

    Returns ``None`` when the records table has no entry for *storage_id*.
    ``ZERO32`` slot positions are skipped; other records are left as hash
    references.
    """
    blob = get_record_from_cold_storage(node, storage_id)
    if blob is None:
        return None
    exprs = [root]
    usable = len(blob) - (len(blob) % 32)
    for offset in range(0, usable, 32):
        slot_id = blob[offset : offset + 32]
        if slot_id == ZERO32:
            continue
        slot_expr = get_expr_from_local_storage(node, slot_id)
        if slot_expr is not None:
            exprs.append(slot_expr)
    return exprs


def handle_storage_request(node: "Node", peer: "Peer", message: Message) -> tuple[bool, str | None]:
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
            expr_id = storage_request.expr_id
            node.logger.debug("Handling STORAGE_GET for %s from %s", expr_id.hex(), peer.address)
            desired = storage_request.payload_type or RESOLUTION_SINGLE

            local_atom = get_expr_from_local_storage(node, expr_id)
            if local_atom is not None:
                if desired == RESOLUTION_RECORD:
                    exprs = _collect_record_exprs(node, local_atom, expr_id)
                    if exprs is None:
                        node.logger.debug(
                            "STORAGE_GET %s requested as record but no records-table entry",
                            expr_id.hex(),
                        )
                        return False, "not a record"
                else:
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
                resp = StorageResponse(
                    code=StorageResponseCode.STORAGE_FOUND,
                    data=encode_payload(exprs),
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
                    increment_peer_metric(peer, "shared_storage_upload", shared_storage_size)
                return True, None

            if expr_id in node.storage_index:
                provider_id = node.storage_index[expr_id]
                provider_bytes = provider_payload_for_id(node, provider_id)
                if provider_bytes is not None:
                    node.logger.debug("Known provider for %s; informing %s", expr_id.hex(), peer.address)
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
                    expr_id.hex(),
                )

            nearest_peer = node.peer_route.closest_peer_for_hash(expr_id)
            if nearest_peer:
                node.logger.debug("Forwarding requester %s to nearest peer for %s", peer.address, expr_id.hex())
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

            if expr_id in node.storage_index:
                return False, f"unknown provider id {node.storage_index[expr_id]} for {expr_id.hex()}"
            return True, None

        case StorageRequestCode.STORAGE_PUT:
            node.logger.debug("Handling STORAGE_PUT for %s from %s", storage_request.expr_id.hex(), peer.address)

            from astreum.storage.admission import is_expr_in_latest_block
            if not is_expr_in_latest_block(node, storage_request.expr_id):
                node.logger.debug(
                    "STORAGE_PUT rejected for %s from %s: not committed",
                    storage_request.expr_id.hex(),
                    peer.address,
                )
                return False, "STORAGE_PUT rejected: expr not committed"

            nearest_peer = node.peer_route.closest_peer_for_hash(storage_request.expr_id)
            is_self_closest = False
            if nearest_peer is None or nearest_peer.address is None:
                is_self_closest = True
            else:
                try:
                    self_distance = xor_distance(storage_request.expr_id, node.storage_public_key_bytes)
                    peer_distance = xor_distance(storage_request.expr_id, nearest_peer.public_key_bytes)
                except Exception as exc:
                    node.logger.debug(
                        "Failed distance comparison for STORAGE_PUT %s: %s",
                        storage_request.expr_id.hex(),
                        exc,
                    )
                    is_self_closest = True
                else:
                    is_self_closest = self_distance <= peer_distance

            if is_self_closest:
                from astreum.storage.admission import get_latest_storage_account
                from astreum.storage.radix import get_from_radix_tree
                from astreum.storage.records import parse_record_new_count

                stored_value = None
                storage_account = get_latest_storage_account(node)
                if storage_account is not None:
                    stored_value = get_from_radix_tree(
                        storage_account.data, node, storage_request.expr_id
                    )
                if parse_record_new_count(node, stored_value) is None:
                    node.logger.debug(
                        "STORAGE_PUT skipped for %s from %s: not a record header",
                        storage_request.expr_id.hex(),
                        peer.address,
                    )
                    return True, None

                node.logger.debug("Storing provider info for %s locally", storage_request.expr_id.hex())
                provider_id = provider_id_for_payload(node, storage_request.data)
                node.storage_index[storage_request.expr_id] = provider_id
                print(
                    "STORAGE_PUT indexed provider expr_id=%s from=%s"
                    % (storage_request.expr_id.hex(), peer.address)
                )
                return True, None
            else:
                node.logger.debug(
                    "Forwarding STORAGE_PUT for %s to nearer peer %s",
                    storage_request.expr_id.hex(),
                    nearest_peer.address,
                )
                fwd_req = StorageRequest(
                    code=StorageRequestCode.STORAGE_PUT,
                    data=storage_request.data,
                    expr_id=storage_request.expr_id,
                    payload_type=storage_request.payload_type,
                )
                req_msg = Message(
                    topic=MessageTopic.STORAGE_REQUEST,
                    body=fwd_req.to_bytes(),
                    sender_public_key_bytes=node.storage_public_key_bytes,
                )
                req_msg.encrypt(nearest_peer.shared_key_bytes)
                enqueue_outgoing(
                    node,
                    nearest_peer.address,
                    message=req_msg,
                    difficulty=nearest_peer.difficulty,
                )
                return True, None

        case _:
            node.logger.debug("Unknown StorageRequestCode %s from %s", storage_request.code, peer.address)
            return False, f"unknown StorageRequestCode {storage_request.code}"

    return True, None
