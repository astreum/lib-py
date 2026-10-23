from __future__ import annotations

from typing import Optional

from astreum.expression import Expr, RESOLUTION_DYNAMIC, RESOLUTION_SINGLE
from astreum.expression.shape import parse_shape, walk_shape
from astreum.storage.exprs.local import get_expr_from_local_storage


def get_expr_by_shape(
    node, expr_id: bytes, shape: bytes, contact=None, block_hash=None
) -> Optional[Expr]:
    """Fetch an expr and the exprs a shape reaches below it.

    One DYNAMIC request asks a provider for everything the shape reaches. The
    provider sends what it holds. A hash the shape reaches that is still not in
    local storage is then fetched as one batch of SINGLE requests, and this
    repeats until nothing is missing, nothing new arrives, or the fetch retries
    run out. Exprs already in local storage are not requested again.

    The exprs arrive in local storage. Read them with the usual ``get_expr``
    calls, which then use no network. A caller that needs fewer exprs than the
    shape reaches should pass a smaller shape.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the root expr.
        shape: The shape that says how far to go below the root.
        contact: Optional ``RelayEndpoint`` to ask directly.
        block_hash: Optional block whose provider holds the exprs.

    Returns:
        The root Expr, or None if it could not be fetched. The exprs below the
        root may still be missing when the fetch ends. A caller reads them with
        its normal path, which fetches what is missing.

    Raises:
        ValueError: If the shape is malformed or longer than
            ``storage_shape_max_bytes``.
    """
    from astreum.storage.exprs.network import (
        _poll_local,
        _resend_to_next_provider,
        get_exprs_from_network,
    )
    from astreum.storage.workers.requests import queue_storage_get

    rule = parse_shape(shape)
    if len(shape) > node.config.storage_shape_max_bytes:
        raise ValueError(
            f"shape is {len(shape)} bytes, the limit is {node.config.storage_shape_max_bytes}"
        )

    def load(h: bytes) -> Optional[Expr]:
        return get_expr_from_local_storage(node, h)

    _, missing = walk_shape(rule, expr_id, load)
    if not missing:
        return load(expr_id)
    if not node.is_connected:
        return load(expr_id)

    scope = {} if block_hash is None else {"block_hash": block_hash}
    if expr_id in missing:
        # Only a scoped call without a contact is routed by the block.
        route = {} if block_hash is None or contact is not None else scope
        if queue_storage_get(
            node, expr_id, RESOLUTION_DYNAMIC, contact=contact, shape=shape, **route
        ):
            if _poll_local(node, [expr_id])[expr_id] is None and contact is None:
                if _resend_to_next_provider(
                    node, [(expr_id, RESOLUTION_DYNAMIC)], block_hash, shape
                ):
                    _poll_local(node, [expr_id])
        if load(expr_id) is None:
            return None

    retries = max(node.config.storage_fetch_retries, 1)
    previous: Optional[list[bytes]] = None
    for _ in range(retries):
        _, missing = walk_shape(rule, expr_id, load)
        if not missing or missing == previous:
            break
        previous = missing
        get_exprs_from_network(
            node,
            [(h, RESOLUTION_SINGLE) for h in missing],
            contact=contact,
            **scope,
        )
    return load(expr_id)
