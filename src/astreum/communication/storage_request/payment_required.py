from typing import TYPE_CHECKING

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_payment_required import encode_storage_payment_required
from astreum.communication.outgoing_queue import enqueue_outgoing

if TYPE_CHECKING:
    from astreum.communication import Node
    from astreum.communication.models.peer import Peer


def _requires_storage_channel(node: "Node", peer: "Peer", next_upload_bytes: int) -> bool:
    """Return True when a peer must use a payment/channel before another object share.

    Args:
        node (Node): The node instance checking the requirement.
        peer (Peer): The peer being checked.
        next_upload_bytes (int): The projected upload size in bytes.

    Returns:
        bool: True if payment/channel is required, False otherwise.
    """
    pending_upload = next_upload_bytes
    if pending_upload <= 0:
        return False

    fair_use_limit = node.config["fair_use_limit"]
    fair_use_ratio = node.config["fair_use_ratio"]
    if fair_use_ratio <= 0:
        return False

    with peer.metrics_lock:
        current_upload = peer.shared_storage_upload
        current_download = peer.shared_storage_download

    projected_upload = current_upload + pending_upload
    if projected_upload < fair_use_limit:
        return False
    if projected_upload <= 0:
        return False

    return (current_download / projected_upload) < fair_use_ratio


def _queue_storage_payment_required(
    node: "Node",
    peer: "Peer",
    expr_id: bytes,
    storage_size_estimate: int,
) -> bool:
    """Queue a STORAGE_PAYMENT_REQUIRED response for a peer.

    Args:
        node (Node): The node instance sending the response.
        peer (Peer): The peer requiring payment.
        expr_id (bytes): The expression ID being requested.
        storage_size_estimate (int): The estimated storage size in bytes.

    Returns:
        bool: True if the response was queued, False otherwise.
    """
    payment_public_key = (getattr(node, "config", {}) or {}).get("storage_public_key_bytes")
    if not isinstance(payment_public_key, (bytes, bytearray)) or len(payment_public_key) != 32:
        node.logger.warning(
            "Cannot send STORAGE_PAYMENT_REQUIRED for %s to %s: relay payment public key is unavailable",
            expr_id.hex(),
            peer.address,
        )
        return False

    try:
        base_storage_fee = int(getattr(node, "storage_request_current_price", 0) or 0)
    except Exception:
        base_storage_fee = 0
    if base_storage_fee < 0:
        base_storage_fee = 0

    try:
        payload = encode_storage_payment_required(
            payment_public_key=bytes(payment_public_key),
            storage_size_estimate=storage_size_estimate,
            base_storage_fee=base_storage_fee,
            hint_peer=None,
        )
    except Exception as exc:
        node.logger.warning(
            "Failed encoding STORAGE_PAYMENT_REQUIRED for %s to %s: %s",
            expr_id.hex(),
            peer.address,
            exc,
        )
        return False

    try:
        response = StorageResponse(
            code=StorageResponseCode.STORAGE_PAYMENT_REQUIRED,
            data=payload,
            expr_id=expr_id,
        )
        msg = Message(
            topic=MessageTopic.STORAGE_RESPONSE,
            body=response.to_bytes(),
            sender_public_key_bytes=node.storage_public_key_bytes,
        )
        msg.encrypt(peer.shared_key_bytes)
        queued = enqueue_outgoing(
            node,
            peer.address,
            message=msg,
            difficulty=peer.difficulty,
        )
    except Exception as exc:
        node.logger.warning(
            "Failed queueing STORAGE_PAYMENT_REQUIRED for %s to %s: %s",
            expr_id.hex(),
            peer.address,
            exc,
        )
        return False

    if queued:
        node.logger.info(
            "Queued STORAGE_PAYMENT_REQUIRED for %s to %s (size_estimate=%s base_storage_fee=%s)",
            expr_id.hex(),
            peer.address,
            storage_size_estimate,
            base_storage_fee,
        )
        return True

    node.logger.debug(
        "Dropped STORAGE_PAYMENT_REQUIRED for %s to %s",
        expr_id.hex(),
        peer.address,
    )
    return False
