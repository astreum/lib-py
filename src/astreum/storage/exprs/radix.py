from __future__ import annotations

from typing import Optional

from astreum.expression import Expr, RESOLUTION_RADIX
from astreum.storage.exprs.local import get_expr_from_local_storage


def get_radix_from_network(
    node, node_hash: bytes, key: bytes, key_bit_offset: int, contact=None, block_hash=None
) -> Optional[Expr]:
    """Fetch a radix node and the nodes on the way to a key with one request.

    One RADIX request asks a provider to walk the tree from ``node_hash``
    towards ``key`` and to send the exprs that a reader needs for each node on
    the way. The provider sends the nodes it holds. It stops at the first node
    it does not hold, so the exprs of the next nodes may not arrive. A reader
    then finds them missing and fetches them with its usual calls, or calls
    this function again from the first missing node.

    The exprs arrive in local storage. Read them with the usual ``get_expr``
    calls, which then use no network.

    Args:
        node: A Node instance providing config and storage access.
        node_hash: The hash of the radix node to start from.
        key: The key that is looked up.
        key_bit_offset: The number of key bits that earlier nodes matched.
        contact: Optional ``RelayEndpoint`` to ask directly.
        block_hash: Optional block whose provider holds the node.

    Returns:
        The root expr of the node, or None if it could not be fetched.

    Raises:
        ValueError: If the offset is outside the key, or the key is longer than
            ``storage_radix_key_max_bytes``.
    """
    from astreum.communication.storage_request.model import encode_radix_param
    from astreum.storage.exprs.network import _poll_local, _resend_to_next_provider
    from astreum.storage.workers.requests import queue_storage_get

    if len(key) > node.config.storage_radix_key_max_bytes:
        raise ValueError(
            f"radix key is {len(key)} bytes, the limit is "
            f"{node.config.storage_radix_key_max_bytes}"
        )
    param = encode_radix_param(key_bit_offset, key)

    root = get_expr_from_local_storage(node, node_hash)
    if root is not None or not getattr(node, "is_connected", False):
        return root

    # Only a scoped call without a contact is routed by the block.
    route = {} if block_hash is None or contact is not None else {"block_hash": block_hash}
    if queue_storage_get(
        node, node_hash, RESOLUTION_RADIX, contact=contact, shape=param, **route
    ):
        if _poll_local(node, [node_hash])[node_hash] is None and contact is None:
            if _resend_to_next_provider(
                node, [(node_hash, RESOLUTION_RADIX)], block_hash, param
            ):
                _poll_local(node, [node_hash])
    return get_expr_from_local_storage(node, node_hash)
