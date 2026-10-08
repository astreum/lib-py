from __future__ import annotations

from typing import Optional, Union

from astreum.expression import Expr, ZERO32
from astreum.storage.exprs.cascade import get_expr
from astreum.storage.exprs.local import get_expr_from_local_storage


def get_expr_list(
    node, root: Union[bytes, Expr], contact=None, block_hash=None
) -> Optional[Expr]:
    """Resolve an Expr and its tail chain using full resolution.

    Fetches the root expression, then walks the ``_tail`` pointers,
    resolving each unresolved ``_tail_hash`` using the same resolution
    chain (hot → cold → network).  Returns a fully materialized list
    with no remaining hash references.

    Args:
        node: A Node instance providing config and storage access.
        root: The content hash of the root expression, or an Expr
            instance that has already been fetched.
        contact: Optional ``(relay_key, address, port)`` passed to every
            network fetch along the chain.
        block_hash: Optional block passed to every network fetch along the
            chain (the tail of a cell is in the block of the cell).

    Returns:
        The root Expr with the tail chain expanded inline, or None
        if any part of the chain cannot be found.
    """
    scope = {} if block_hash is None else {"block_hash": block_hash}
    if isinstance(root, Expr):
        expr = root
    else:
        expr = get_expr(node, root, contact=contact, **scope)
        if expr is None:
            return None

    current = expr
    while current is not None and current._tag == "link":
        if current._tail_hash is not None:
            if current._tail_hash == ZERO32:
                current._tail = None
                current._tail_hash = None
            else:
                resolved = get_expr(
                    node, current._tail_hash, contact=contact, **scope
                )
                if resolved is None:
                    return None
                current._tail = resolved
                current._tail_hash = None
        current = current._tail
    return expr


def get_expr_list_from_local_storage(node, root: Union[bytes, Expr]) -> Optional[Expr]:
    """Resolve an Expr and its tail chain from local storage.

    Starts from a root hash (or an already-fetched Expr) and walks
    the ``_tail`` pointers, resolving each unresolved ``_tail_hash``
    by fetching from local storage (hot → cold).  Only the tail chain
    is resolved — ``_head`` hashes are left untouched.

    Args:
        node: A Node instance providing config and storage access.
        root: The content hash of the root expression, or an Expr
            instance that has already been fetched.

    Returns:
        The root Expr with the tail chain expanded inline, or None
        if any part of the chain cannot be found in local storage.
    """
    if isinstance(root, Expr):
        expr = root
    else:
        expr = get_expr_from_local_storage(node, root)
        if expr is None:
            return None

    current = expr
    while current is not None and current._tag == "link":
        if current._tail_hash is not None:
            resolved = get_expr_from_local_storage(node, current._tail_hash)
            if resolved is None:
                break
            current._tail = resolved
            current._tail_hash = None
        current = current._tail

    return expr
