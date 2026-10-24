from __future__ import annotations

from time import sleep
from typing import Optional

from astreum.expression import RESOLUTION_RADIX


def _is_readable(node, node_hash: bytes, block_hash: Optional[bytes]) -> bool:
    """Return True if the whole radix node reads from local storage.

    Args:
        node: A Node instance providing storage access.
        node_hash: The hash of the radix node.
        block_hash: The block whose record holds the node.

    Returns:
        bool: True if the node and the cells its reader needs are all local.
    """
    from astreum.storage.radix.node import get_radix_node_from_storage

    try:
        get_radix_node_from_storage(node, node_hash, block_hash=block_hash, local=True)
    except ValueError:
        return False
    return True


def _is_request_finished(node, expr_id: bytes) -> bool:
    """Return True if no request for ``expr_id`` is waiting or in flight.

    Args:
        node: A Node instance with the request registry.
        expr_id: The hash that was requested.

    Returns:
        bool: True if the request was answered or dropped. False if the node
        has no registry.
    """
    from astreum.storage.requests import has_expr_req

    if getattr(node, "expr_requests_lock", None) is None:
        return False
    buffered = getattr(node, "storage_request_buffered", ())
    return not has_expr_req(node, expr_id) and expr_id not in buffered


def _wait_until_readable(node, node_hash: bytes, block_hash: Optional[bytes]) -> bool:
    """Wait for the whole radix node to arrive in local storage.

    The wait ends when the node reads in full, when the request is finished
    and the node still does not read in full, or when the fetch retries run out.

    Args:
        node: A Node instance providing config and storage access.
        node_hash: The hash of the radix node.
        block_hash: The block whose record holds the node.

    Returns:
        bool: True if the node reads in full from local storage.
    """
    interval = node.config.storage_fetch_interval
    retries = node.config.storage_fetch_retries
    if interval <= 0 or retries <= 0:
        return _is_readable(node, node_hash, block_hash)
    for _ in range(retries):
        if _is_readable(node, node_hash, block_hash):
            return True
        if _is_request_finished(node, node_hash):
            return _is_readable(node, node_hash, block_hash)
        sleep(interval)
    return _is_readable(node, node_hash, block_hash)


def get_radix_from_network(
    node, node_hash: bytes, key: bytes, key_bit_offset: int, contact=None, block_hash=None
) -> bool:
    """Fetch a radix node and the nodes on the way to a key with one request.

    One RADIX request asks a provider to walk the tree from ``node_hash``
    towards ``key`` and to send the exprs that a reader needs for each node on
    the way. The provider sends the nodes it holds. It stops at the first node
    it does not hold, so the exprs of the next nodes may not arrive. A reader
    then fetches them with its usual calls, or calls this function again from
    the first missing node.

    No request is sent if the whole node already reads from local storage. The
    exprs arrive in local storage. Read them with the usual calls, which then
    use no network.

    Args:
        node: A Node instance providing config and storage access.
        node_hash: The hash of the radix node to start from.
        key: The key that is looked up.
        key_bit_offset: The number of key bits that earlier nodes matched.
        contact: Optional ``RelayEndpoint`` to ask directly.
        block_hash: Optional block whose provider holds the node.

    Returns:
        bool: True if the whole node reads from local storage at the end.

    Raises:
        ValueError: If the offset is outside the key, or the key is longer than
            ``storage_radix_key_max_bytes``.
    """
    from astreum.communication.storage_request.model import encode_radix_param
    from astreum.storage.exprs.network import _resend_to_next_provider
    from astreum.storage.workers.requests import queue_storage_get

    if len(key) > node.config.storage_radix_key_max_bytes:
        raise ValueError(
            f"radix key is {len(key)} bytes, the limit is "
            f"{node.config.storage_radix_key_max_bytes}"
        )
    param = encode_radix_param(key_bit_offset, key)

    if _is_readable(node, node_hash, block_hash):
        return True
    if not getattr(node, "is_connected", False):
        return False

    # Only a scoped call without a contact is routed by the block.
    route = {} if block_hash is None or contact is not None else {"block_hash": block_hash}
    if not queue_storage_get(
        node, node_hash, RESOLUTION_RADIX, contact=contact, shape=param, **route
    ):
        return False
    if _wait_until_readable(node, node_hash, block_hash):
        return True
    if contact is None and _resend_to_next_provider(
        node, [(node_hash, RESOLUTION_RADIX)], block_hash, param
    ):
        return _wait_until_readable(node, node_hash, block_hash)
    return False
