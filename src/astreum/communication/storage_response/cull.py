from typing import Callable, Dict, List, Optional

from astreum.expression import (
    RESOLUTION_FULL,
    RESOLUTION_LIST,
    RESOLUTION_RECORD,
    ZERO32,
    Expr,
)


def _child_hashes(expr: Expr) -> List[bytes]:
    """Return the head and tail hashes of a link (empty for other exprs)."""
    if expr.base != "link":
        return []
    hashes: List[bytes] = []
    for stored, child in ((expr.head_hash, expr.head), (expr.tail_hash, expr.tail)):
        if stored is not None:
            h = stored
        elif child is not None:
            h = child.hash()
        else:
            continue
        if h != ZERO32:
            hashes.append(h)
    return hashes


def cull_to_resolution(
    root: Expr,
    exprs: List[Expr],
    resolution: Optional[int],
    resolve_local: Optional[Callable[[bytes], Optional[Expr]]] = None,
) -> List[Expr]:
    """Keep only the delivered exprs within the requested ``resolution``.

    SINGLE keeps the root; LIST the root and its tail chain; FULL and RECORD
    everything reachable from the root through head and tail links. Only exprs
    present in ``exprs`` can be kept, each at most once, root first. Anything
    else in the reply is dropped.

    ``resolve_local`` (used for RECORD) lets the walk step through nodes the
    requester already holds but the reply did not carry: a record reply is the
    root plus its slot exprs, and a slot may sit below such a node. Those nodes
    are traversed, never returned. The walk stops once every delivered expr
    has been placed.
    """
    by_hash: Dict[bytes, Expr] = {}
    for expr in exprs:
        by_hash.setdefault(expr.hash(), expr)

    root_hash = root.hash()
    kept: List[Expr] = [root]
    seen = {root_hash}

    if resolution is None or resolution < RESOLUTION_LIST:
        return kept

    if resolution < RESOLUTION_FULL:
        current = root
        while current.base == "link":
            tail_hash = current.tail_hash
            if tail_hash is None and current.tail is not None:
                tail_hash = current.tail.hash()
            if tail_hash is None or tail_hash == ZERO32 or tail_hash in seen:
                break
            nxt = by_hash.get(tail_hash)
            if nxt is None:
                break
            seen.add(tail_hash)
            kept.append(nxt)
            current = nxt
        return kept

    use_local = resolve_local is not None and resolution >= RESOLUTION_RECORD
    remaining = len(by_hash) - 1
    stack = [root]
    while stack and remaining > 0:
        current = stack.pop()
        for h in _child_hashes(current):
            if h in seen:
                continue
            child = by_hash.get(h)
            if child is not None:
                seen.add(h)
                kept.append(child)
                remaining -= 1
                stack.append(child)
            elif use_local:
                local = resolve_local(h)
                if local is not None:
                    seen.add(h)
                    stack.append(local)
    return kept
