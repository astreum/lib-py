from __future__ import annotations

from time import time
from typing import Optional


def get_expr_from_hot_storage(node, expr_id: bytes) -> Optional["Expr"]:
    """Retrieve an Expr from the in-memory hot storage cache.

    Looks up ``expr_id`` in the node's hot storage dict and refreshes
    the access timestamp on a hit.

    Args:
        node: A Node instance providing config and storage access.
        expr_id: The content hash of the expression to retrieve.

    Returns:
        The cached Expr if found, or None.
    """
    with node.hot_storage_lock:
        expr = node.hot_storage.get(expr_id)
        if expr is not None:
            node.hot_storage_timestamps[expr_id] = time()
            return expr
        return None


def put_expr_in_hot_storage(node, expr: "Expr") -> bool:
    """Store an Expr in the in-memory hot storage cache with LRU eviction.

    Recursively stores Link children before their parent so that child
    hashes are stable. Checks the expression size against
    ``hot_storage_limit``. If the cache is full, evicts the oldest
    entries (by access timestamp) until space is available.

    Args:
        node: A Node instance providing config and storage access.
        expr: The expression to cache.

    Returns:
        True if the expression was stored, False if it exceeds the
        hot storage limit.
    """

    # Descend into Link children first so child hashes are stable before parent
    if expr.base == "link":
        if expr._head is not None:
            put_expr_in_hot_storage(node, expr._head)
        if expr._tail is not None:
            put_expr_in_hot_storage(node, expr._tail)

    key = expr.hash()
    with node.hot_storage_lock:
        hot_limit = node.config["hot_storage_limit"]
        size = expr.size()
        if size > hot_limit:
            return False

        existing = node.hot_storage.get(key)
        existing_size = existing.size() if existing is not None else 0
        projected = node.hot_storage_size - existing_size + size

        while projected > hot_limit:
            timestamps = node.hot_storage_timestamps
            if not timestamps:
                break
            if existing is not None and len(timestamps) == 1 and key in timestamps:
                break

            victim_key = None
            victim_ts = None
            for candidate_key, candidate_ts in timestamps.items():
                if existing is not None and candidate_key == key:
                    continue
                if victim_ts is None or candidate_ts < victim_ts:
                    victim_key = candidate_key
                    victim_ts = candidate_ts

            if victim_key is None:
                break

            victim = node.hot_storage.pop(victim_key, None)
            timestamps.pop(victim_key, None)
            if victim is not None:
                node.hot_storage_size -= victim.size()

            projected = node.hot_storage_size - existing_size + size

        if projected > hot_limit:
            return False

        if existing is not None:
            node.hot_storage_size -= existing_size

        node.hot_storage[key] = expr
        node.hot_storage_timestamps[key] = time()
        node.hot_storage_size += size
        return True
