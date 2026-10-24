from __future__ import annotations

from typing import Optional

from astreum.expression import Expr, NIL, RESOLUTION_SINGLE, ZERO32
from astreum.storage.cold import get_expr_from_cold_storage
from astreum.storage.exprs.hot import get_expr_from_hot_storage
from astreum.storage.exprs.network import get_expr_from_network


def _builtin_type_symbol(expr_id: bytes) -> Optional[Expr]:
    from astreum.expression.expr import _BUILTIN_TYPE_HASH

    name = _BUILTIN_TYPE_HASH.get(expr_id)
    if name is None:
        return None
    symbol = Expr("symbol", value=name)
    return symbol if symbol.hash() == expr_id else None


def get_expr(
    node, expr_id: bytes, contact=None, block_hash=None, local: bool = False
) -> Optional[Expr]:
    """Retrieve an Expr using cascading resolution (hot → cold → network).

    Tries the in-memory hot cache first, then cold storage on disk,
    and finally falls back to a P2P network fetch.  Returns a shallow
    expression with hash references left unresolved — use
    ``get_expr_list`` or ``get_expr_full`` for deep resolution.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to retrieve.
        contact: Optional ``RelayEndpoint``. Only the network
            step uses it: the GET goes straight to that peer instead of being
            routed. Hot and cold storage are still checked first.
        block_hash: Optional block whose provider holds *expr_id*. Only the
            network step uses it: the GET is routed by the block and asks for
            *expr_id* there.
        local: If True, skip the network step. A miss in hot and cold storage
            returns None.

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

    # The symbol of a builtin type is known, and no block holds it: an int or
    # a float names it by hash, so it is never asked for.
    known = _builtin_type_symbol(expr_id)
    if known is not None:
        return known

    if local:
        return None

    scope = {} if block_hash is None else {"block_hash": block_hash}
    expr = get_expr_from_network(
        node, expr_id, RESOLUTION_SINGLE, contact=contact, **scope
    )
    if expr is not None:
        return expr

    return None
