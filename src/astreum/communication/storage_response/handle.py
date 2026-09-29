from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import increment_peer_metric
from astreum.communication.storage_response.cull import cull_to_resolution
from astreum.communication.storage_response.storage_found import decode_found_page
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_payment_required import decode_storage_payment_required
from astreum.communication.storage_response.storage_provider import decode_storage_provider
from astreum.communication.storage_response.retry import _retry_pending_storage_get_via_peer_contact
from astreum.expression import Expr
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.storage.exprs import get_expr_from_local_storage, put_expr_in_hot_storage
from astreum.storage.requests import (
    StageResult,
    has_expr_req,
    pop_expr_req,
    stage_found_page,
)

if TYPE_CHECKING:
    from astreum.communication import Node
    from astreum.communication.models.peer import Peer

def handle_storage_response(node: "Node", peer: "Peer", message: Message) -> tuple[bool, str | None]:
    if message.content is None:
        node.logger.debug("STORAGE_RESPONSE from %s missing content", peer.address)
        return False, "missing content"

    try:
        storage_response = StorageResponse.from_bytes(message.content)
    except Exception as exc:
        node.logger.debug("Error decoding STORAGE_RESPONSE from %s: %s", peer.address, exc)
        return False, "decode failed"

    if not has_expr_req(node, storage_response.expr_id):
        return True, None

    match storage_response.code:
        case StorageResponseCode.STORAGE_FOUND:
            payload = storage_response.data
            if not payload:
                node.logger.debug(
                    "STORAGE_FOUND payload for %s missing content",
                    storage_response.expr_id.hex(),
                )
                return False, "STORAGE_FOUND payload missing content"

            try:
                page, total, page_exprs = decode_found_page(payload)
            except Exception as exc:
                node.logger.debug(
                    "Invalid STORAGE_FOUND payload for %s: %s",
                    storage_response.expr_id.hex(),
                    exc,
                )
                return False, "invalid STORAGE_FOUND payload"

            result, request = stage_found_page(
                node,
                storage_response.expr_id,
                peer.public_key_bytes,
                page,
                total,
                page_exprs,
            )
            if result is StageResult.IGNORED:
                node.logger.debug(
                    "STORAGE_FOUND page %d/%d for %s ignored",
                    page,
                    total,
                    storage_response.expr_id.hex(),
                )
                return True, None
            if result is StageResult.STAGED:
                return True, None

            # Every page has arrived; the request is already popped. Nothing
            # has touched hot storage yet.
            root_id = storage_response.expr_id
            exprs = [e for n in sorted(request.pages) for e in request.pages[n]]

            from astreum.storage.admission import is_expr_in_latest_block
            if not is_expr_in_latest_block(node, root_id):
                node.logger.debug(
                    "STORAGE_FOUND rejected for %s: uncommitted data",
                    root_id.hex(),
                )
                return False, "uncommitted data rejected"

            root = next((e for e in exprs if e.hash() == root_id), None)
            if root is None:
                node.logger.debug(
                    "STORAGE_FOUND root ID mismatch (expected=%s)", root_id.hex()
                )
                return False, "STORAGE_FOUND root ID mismatch"

            kept = cull_to_resolution(
                root,
                exprs,
                request.payload_type,
                resolve_local=lambda h: get_expr_from_local_storage(node, h),
            )
            if len(kept) < len(exprs):
                node.logger.debug(
                    "STORAGE_FOUND for %s: dropped %d of %d exprs outside resolution %s",
                    root_id.hex(),
                    len(exprs) - len(kept),
                    len(exprs),
                    request.payload_type,
                )

            increment_peer_metric(
                peer,
                "shared_storage_download",
                sum(len(encode_expr_to_bytes(expr)) for expr in kept),
            )
            hot_store_failures = 0
            for expr in kept[1:]:
                if not put_expr_in_hot_storage(node, expr):
                    hot_store_failures += 1
            if not put_expr_in_hot_storage(node, root):
                hot_store_failures += 1
            if hot_store_failures:
                return (
                    False,
                    f"failed hot storage set for {root_id.hex()} count={hot_store_failures}",
                )
            return True, None

        case StorageResponseCode.STORAGE_PROVIDER:
            try:
                storage_key_bytes, relay_key_bytes, provider_address, provider_port = decode_storage_provider(
                    storage_response.data
                )
            except Exception as exc:
                node.logger.debug("Invalid STORAGE_PROVIDER payload from %s: %s", peer.address, exc)
                return False, "invalid STORAGE_PROVIDER payload"

            _retry_pending_storage_get_via_peer_contact(
                node,
                expr_id=storage_response.expr_id,
                peer_contact=(relay_key_bytes, provider_address, provider_port),
            )
            return True, None

        case StorageResponseCode.STORAGE_PAYMENT_REQUIRED:
            try:
                payment_public_key, storage_size_estimate, base_storage_fee, hint_peer = (
                    decode_storage_payment_required(storage_response.data)
                )
            except Exception as exc:
                node.logger.debug(
                    "Invalid STORAGE_PAYMENT_REQUIRED payload from %s: %s",
                    peer.address,
                    exc,
                )
                return False, "invalid STORAGE_PAYMENT_REQUIRED payload"
            node.logger.debug(
                "Received STORAGE_PAYMENT_REQUIRED from %s (payment_key=%s, size_estimate=%s, base_storage_fee=%s, hint=%s)",
                peer.address,
                payment_public_key.hex(),
                storage_size_estimate,
                base_storage_fee,
                hint_peer,
            )

            has_local_payment_key = bool(
                getattr(node, "storage_secret_key", None)
                or (getattr(node, "config", {}) or {}).get("storage_secret_key")
            )
            if has_local_payment_key:
                return True, None

            if hint_peer is not None:
                node.logger.info(
                    "STORAGE_PAYMENT_REQUIRED for %s from %s but no local payment key; trying hint %s:%s",
                    storage_response.expr_id.hex(),
                    peer.address,
                    hint_peer[1],
                    hint_peer[2],
                )
                if _retry_pending_storage_get_via_peer_contact(
                    node,
                    expr_id=storage_response.expr_id,
                    peer_contact=hint_peer,
                ):
                    return True, None
                node.logger.info(
                    "Hint retry failed for %s from %s; ending request",
                    storage_response.expr_id.hex(),
                    peer.address,
                )

            else:
                node.logger.info(
                    "STORAGE_PAYMENT_REQUIRED for %s from %s and no local payment key or hint; ending request",
                    storage_response.expr_id.hex(),
                    peer.address,
                )

            pop_expr_req(node, storage_response.expr_id)
            return True, None

        case _:
            return False, f"unknown StorageResponseCode {storage_response.code}"
