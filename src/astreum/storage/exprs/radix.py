from __future__ import annotations

from time import sleep
from typing import Optional

from astreum.expression import RESOLUTION_RADIX_ALL, RESOLUTION_RADIX_KEY


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


def get_radix_key_from_network(
    node,
    node_hash: bytes,
    key: bytes,
    key_bit_offset: int,
    contact=None,
    block_hash=None,
    value_shape: bytes = b"",
) -> bool:
    """Fetch a radix node and the nodes on the way to a key with one request.

    One RADIX_KEY request asks a provider to walk the tree from ``node_hash``
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
        value_shape: The shape the provider applies to the value at the key.
            Empty for none.

    Returns:
        bool: True if the whole node reads from local storage at the end.

    Raises:
        ValueError: If the offset is outside the key, the key is longer than
            ``storage_radix_key_max_bytes``, or the value shape cannot be
            parsed or is longer than ``storage_shape_max_bytes``.
    """
    from astreum.communication.storage_request.model import encode_radix_key_param
    from astreum.storage.exprs.network import _resend_to_next_provider
    from astreum.storage.workers.requests import queue_storage_get

    if len(key) > node.config.storage_radix_key_max_bytes:
        raise ValueError(
            f"radix key is {len(key)} bytes, the limit is "
            f"{node.config.storage_radix_key_max_bytes}"
        )
    if len(value_shape) > node.config.storage_shape_max_bytes:
        raise ValueError(
            f"value shape is {len(value_shape)} bytes, the limit is "
            f"{node.config.storage_shape_max_bytes}"
        )
    param = encode_radix_key_param(key_bit_offset, key, value_shape)

    if _is_readable(node, node_hash, block_hash):
        return True
    if not getattr(node, "is_connected", False):
        return False

    # Only a scoped call without a contact is routed by the block.
    route = {} if block_hash is None or contact is not None else {"block_hash": block_hash}
    if not queue_storage_get(
        node, node_hash, RESOLUTION_RADIX_KEY, contact=contact, shape=param, **route
    ):
        return False
    if _wait_until_readable(node, node_hash, block_hash):
        return True
    if contact is None and _resend_to_next_provider(
        node, [(node_hash, RESOLUTION_RADIX_KEY)], block_hash, param
    ):
        return _wait_until_readable(node, node_hash, block_hash)
    return False


def _wait_until_settled(node, targets: list[tuple[bytes, Optional[bytes]]]) -> bool:
    """Wait until each radix node reads in full or has no request in flight.

    The wait ends when every node is settled, or when the fetch retries run out.

    Args:
        node: A Node instance providing config and storage access.
        targets: ``(hash, block)`` of each radix node that was requested.

    Returns:
        bool: True if every node is settled at the end.
    """
    interval = node.config.storage_fetch_interval
    retries = node.config.storage_fetch_retries

    def settled() -> bool:
        return all(
            _is_request_finished(node, h) or _is_readable(node, h, block)
            for h, block in targets
        )

    if interval <= 0 or retries <= 0:
        return settled()
    for _ in range(retries):
        if settled():
            return True
        sleep(interval)
    return settled()


def get_radix_all_from_network(
    node,
    node_hash: bytes,
    value_shape: bytes = b"",
    contact=None,
    block_hash=None,
) -> bool:
    """Fetch all nodes of a radix tree, and their values, in rounds of requests.

    Each round reads the tree from local storage, from ``node_hash`` down, and
    lists the nodes that local storage does not hold in full. It sends one
    RADIX_ALL request for each of these nodes, scoped to its block. The
    requests leave in batches. A provider answers with the nodes below the
    requested node that are new in its block, and with the exprs that the
    value shape reaches from the value of each node. A child that is a stored
    ref names a node of an older block, so the next round asks for it there.
    A provider may also cut a large reply. The next round asks for the nodes
    after the cut.

    The loop ends when no node is missing, or when a round brings no new node.

    A value that a provider does not hold stays missing. The reader of the
    value fetches it with its usual calls.

    Args:
        node: A Node instance providing config and storage access.
        node_hash: The hash of the radix node to start from.
        value_shape: The shape the provider applies to the value of each node.
            Empty for none.
        contact: Optional ``RelayEndpoint`` to ask directly.
        block_hash: Optional block whose provider holds the node.

    Returns:
        bool: True if all nodes below ``node_hash`` read from local storage at
        the end.

    Raises:
        ValueError: If the value shape cannot be parsed or is longer than
            ``storage_shape_max_bytes``.
    """
    from astreum.communication.storage_request.model import encode_radix_all_param
    from astreum.storage.radix.tree.walk import collect_radix_all
    from astreum.storage.workers.requests import queue_storage_get

    if len(value_shape) > node.config.storage_shape_max_bytes:
        raise ValueError(
            f"value shape is {len(value_shape)} bytes, the limit is "
            f"{node.config.storage_shape_max_bytes}"
        )
    param = encode_radix_all_param(value_shape)

    last = None
    confirmed = False
    while True:
        _, missing = collect_radix_all(node, node_hash, block_hash)
        if not missing:
            return True
        if not getattr(node, "is_connected", False):
            return False
        if missing == last:
            # The handler may still be storing the last reply. Look once more.
            if confirmed:
                return False
            confirmed = True
            sleep(max(node.config.storage_fetch_interval, 0))
            continue
        confirmed = False
        last = missing

        queued = []
        for missing_hash, missing_block in missing:
            # Only a scoped call without a contact is routed by the block.
            route = (
                {} if missing_block is None or contact is not None
                else {"block_hash": missing_block}
            )
            if queue_storage_get(
                node, missing_hash, RESOLUTION_RADIX_ALL, contact=contact, shape=param, **route
            ):
                queued.append((missing_hash, missing_block))
        if not queued:
            return False
        _wait_until_settled(node, queued)
