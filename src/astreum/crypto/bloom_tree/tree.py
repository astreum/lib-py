from __future__ import annotations

from collections.abc import Callable

from astreum.crypto.bloom_tree.node import BloomNode
from astreum.crypto.bloom_filter import bloom_insert, bloom_test
from astreum.expression import ZERO32
from astreum.storage.exprs import get_expr


class BloomTree:
    """Binary bloom tree for a 1024-block chunk."""

    def __init__(self, root_hash=None, astreum_node=None, contact=None):
        self.root: BloomNode | None = None
        self._nodes: dict[bytes, BloomNode] = {}  # expr_id -> node
        # (relay_key, address, port) of the peer holding uncommitted nodes.
        self.contact = contact
        if root_hash and root_hash != ZERO32 and astreum_node:
            from astreum.crypto.bloom_tree.expr import bloom_node_from_expr
            expr = get_expr(astreum_node, root_hash, contact=contact)
            if expr is not None:
                self.root = bloom_node_from_expr(expr)

    def insert(self, offset: int, variants: list[bytes], node=None) -> None:
        """Create leaf at offset with start_hash=None, insert variants along path."""
        if self.root is None:
            self.root = BloomNode(level=0)

        self._walk(self.root, offset, 0, 1024, variants, node, level=0)

    def _walk(self, node: BloomNode, offset: int, lo: int, hi: int,
              variants: list[bytes], astreum_node, level: int) -> None:
        node._expr = None
        for v in variants:
            bloom_insert(node.filter, v)
        self._nodes[node.expr().hash()] = node

        if node.is_leaf:
            return

        mid = (lo + hi) // 2
        child_level = level + 1
        if offset < mid:
            if node.left is None:
                node.left = self._resolve_child(node, offset < mid, astreum_node)
            self._walk(node.left, offset, lo, mid, variants, astreum_node, child_level)
        else:
            if node.right is None:
                node.right = self._resolve_child(node, offset >= mid, astreum_node)
            self._walk(node.right, offset, mid, hi, variants, astreum_node, child_level)

    def _resolve_child(self, parent: BloomNode, is_left: bool, astreum_node) -> BloomNode:
        """Resolve a child — create new or fetch from storage."""
        level = parent.level + 1
        child_hash = parent._left_hash if is_left else parent._right_hash
        if child_hash and astreum_node:
            expr = get_expr(astreum_node, child_hash, contact=self.contact)
            if expr is not None:
                from astreum.crypto.bloom_tree.expr import bloom_node_from_expr
                return bloom_node_from_expr(expr)
        return BloomNode(level=level)

    def set_leaf_start_hash(self, offset: int, block_hash: bytes) -> None:
        """Set start_hash on an existing leaf at offset."""
        if self.root is None:
            return
        self._set_leaf(self.root, offset, 0, 1024, block_hash)

    def _set_leaf(self, node: BloomNode, offset: int, lo: int, hi: int,
                  block_hash: bytes) -> None:
        if node.is_leaf:
            node._expr = None
            node.start_hash = block_hash
            node.filter.start_hash = block_hash
            self._nodes[node.expr().hash()] = node
            return
        mid = (lo + hi) // 2
        child = node.left if offset < mid else node.right
        if child is not None:
            next_lo = lo if offset < mid else mid
            next_hi = mid if offset < mid else hi
            self._set_leaf(child, offset, next_lo, next_hi, block_hash)


def bloom_search(node: BloomNode | None, element: bytes) -> list[bytes]:
    """Search the bloom tree for `element`. Returns list of leaf block hashes
    where the element may be present. Empty list = definitely absent."""
    if node is None:
        return []
    if not bloom_test(node.filter, element):
        return []
    if node.is_leaf:
        return [node.start_hash] if node.start_hash else []

    results: list[bytes] = []
    if node.left is not None:
        results.extend(bloom_search(node.left, element))
    if node.right is not None:
        results.extend(bloom_search(node.right, element))
    return results


def bloom_search_storage(root_hash: bytes, element: bytes, astreum_node) -> list[bytes | None]:
    """Search a sealed era's bloom tree from storage. Fetches nodes on demand.
    root_hash = the era root BloomNode expr hash.
    astreum_node = the Astreum P2P/storage Node (has get_expr).
    Returns list of leaf start_hashes (None = leaf matched but has no start_hash)."""
    from astreum.crypto.bloom_tree.expr import bloom_node_from_expr

    root_expr = get_expr(astreum_node, root_hash)
    if root_expr is None:
        return []
    root = bloom_node_from_expr(root_expr)

    return _search_storage(root, element, astreum_node)


def _search_storage(bloom_node: BloomNode, element: bytes, astreum_node) -> list[bytes | None]:
    if not bloom_test(bloom_node.filter, element):
        return []
    if bloom_node.is_leaf:
        return [bloom_node.start_hash]  # None means "match but no block pointer"

    from astreum.crypto.bloom_tree.expr import bloom_node_from_expr

    results: list[bytes | None] = []
    if bloom_node._left_hash:
        left_expr = get_expr(astreum_node, bloom_node._left_hash)
        if left_expr is not None:
            left = bloom_node_from_expr(left_expr)
            results.extend(_search_storage(left, element, astreum_node))
    if bloom_node._right_hash:
        right_expr = get_expr(astreum_node, bloom_node._right_hash)
        if right_expr is not None:
            right = bloom_node_from_expr(right_expr)
            results.extend(_search_storage(right, element, astreum_node))
    return results
