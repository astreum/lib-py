from __future__ import annotations

from typing import Optional

from astreum.expression import Expr, NIL, RESOLUTION_SINGLE, ZERO32
from astreum.storage.cold import get_expr_from_cold_storage
from astreum.storage.exprs.hot import get_expr_from_hot_storage
from astreum.storage.exprs.network import get_expr_from_network


def get_expr(node, expr_id: bytes, contact=None) -> Optional[Expr]:
    """Retrieve an Expr using cascading resolution (hot → cold → network).

    Tries the in-memory hot cache first, then cold storage on disk,
    and finally falls back to a P2P network fetch.  Returns a shallow
    expression with hash references left unresolved — use
    ``get_expr_list`` or ``get_expr_full`` for deep resolution.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to retrieve.
        contact: Optional ``(relay_key, address, port)``. Only the network
            step uses it: the GET goes straight to that peer instead of being
            routed. Hot and cold storage are still checked first.

    Returns:
        The Expr if found, or None.
    """
    if expr_id == ZERO32:
        return NIL

    expr = get_expr_from_hot_storage(node, expr_id)
    if expr is not None:
        return expr

    expr = get_expr_from_cold_storage(node, expr_id)
    if expr is not None:
        return expr

    expr = get_expr_from_network(node, expr_id, RESOLUTION_SINGLE, contact=contact)
    if expr is not None:
        return expr

    return None
