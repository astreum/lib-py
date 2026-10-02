from typing import TYPE_CHECKING, Tuple

from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.requests import get_expr_req_payload
from astreum.storage.workers.requests import queue_storage_get

if TYPE_CHECKING:
    from astreum.communication import Node


def _retry_pending_storage_get_via_peer_contact(
    node: "Node",
    *,
    expr_id: bytes,
    peer_contact: Tuple[bytes, str, int],
) -> bool:
    """Retry a pending STORAGE_GET via a provider/hint peer contact.

    The GET goes through the request buffer, so redirects to the same provider
    share a datagram. The pending request already exists, so it is queued as
    ``preclaimed``. Returns False when the GET could not be queued.
    """
    payload_type = get_expr_req_payload(node, expr_id)
    if payload_type is None:
        payload_type = RESOLUTION_SINGLE

    try:
        return queue_storage_get(
            node,
            expr_id,
            payload_type,
            contact=peer_contact,
            preclaimed=True,
        )
    except Exception as exc:
        provider_address, provider_port = peer_contact[1], peer_contact[2]
        node.logger.warning(
            "Failed retrying STORAGE_GET for %s via %s:%s: %s",
            expr_id.hex(),
            provider_address,
            provider_port,
            exc,
        )
        return False
