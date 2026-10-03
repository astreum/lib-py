from __future__ import annotations

from typing import Optional, Union

from astreum.expression import Expr
from astreum.storage.exprs.cascade import get_expr
from astreum.storage.exprs.local import get_expr_from_local_storage


def get_expr_full(node, expr_id: bytes, contact=None) -> Optional[Expr]:
    """Resolve an Expr and all transitive link hashes using full resolution.

    Fetches the root expression, then recursively resolves every
    ``_head_hash`` / ``_tail_hash`` using the same resolution chain
    (hot → cold → network).  Returns a fully materialized expression
    tree with no remaining hash references.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the root expression.
        contact: Optional ``(relay_key, address, port)`` passed to every
            network fetch in the tree.

    Returns:
        The fully resolved Expr with all links expanded inline, or None
        if any part of the tree cannot be found.
    """
    expr = get_expr(node, expr_id, contact=contact)
    if expr is None:
        return None
    if not expr._tag == "link":
        return expr

    if expr._head is None and expr._head_hash is not None:
        head = get_expr_full(node, expr._head_hash, contact=contact)
        if head is None:
            return None
        expr._head = head
        expr._head_hash = None

    if expr._tail is None and expr._tail_hash is not None:
        tail = get_expr_full(node, expr._tail_hash, contact=contact)
        if tail is None:
            return None
        expr._tail = tail
        expr._tail_hash = None

    return expr


def get_expr_full_from_local_storage(node, root: Union[bytes, Expr]) -> Optional[Expr]:
    """Resolve an Expr and all transitive link hashes from local storage.

    Starts from a root hash (or an already-fetched Expr) and iteratively
    resolves every unresolved ``_head_hash`` / ``_tail_hash`` by fetching
    from local storage (hot → cold).  Returns a fully materialized
    expression tree with no remaining hash references.

    Args:
        node: A Node instance providing config and storage access.
        root: The content hash of the root expression, or an Expr
            instance that has already been fetched.

    Returns:
        The fully resolved Expr with all links expanded inline, or None
        if any part of the tree cannot be found in local storage.
    """
    if isinstance(root, Expr):
        expr = root
    else:
        expr = get_expr_from_local_storage(node, root)
        if expr is None:
            return None
    if expr._tag != "link":
        return expr

    def _resolve(e: Expr) -> Expr:
        changed = True
        while changed:
            changed = False
            if e._tag != "link":
                break
            if e._head is None and e._head_hash is not None:
                head = get_expr_from_local_storage(node, e._head_hash)
                if head is not None:
                    e._head = _resolve(head)
                    e._head_hash = None
                    changed = True
            if e._tail is None and e._tail_hash is not None:
                tail = get_expr_from_local_storage(node, e._tail_hash)
                if tail is not None:
                    e._tail = _resolve(tail)
                    e._tail_hash = None
                    changed = True
        return e

    return _resolve(expr)
