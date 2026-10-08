from __future__ import annotations

from typing import Optional

from astreum.storage.cold import get_expr_from_cold_storage
from astreum.storage.exprs.hot import get_expr_from_hot_storage


def get_expr_from_local_storage(node, expr_id: bytes) -> Optional["Expr"]:
    """Retrieve an Expr from local storage (hot → cold).

    Tries the in-memory hot cache first. If not found, falls back to
    cold storage on disk.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to retrieve.

    Returns:
        The Expr if found in local storage, or None.
    """
    expr = get_expr_from_hot_storage(node, expr_id)
    if expr is not None:
        return expr
    return get_expr_from_cold_storage(node, expr_id)
