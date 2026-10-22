from __future__ import annotations

from collections.abc import Callable

from astreum.crypto.bloom_tree.node import BloomNode
from astreum.crypto.bloom_filter import bloom_insert, bloom_test
from astreum.expression import Expr, ZERO32
from astreum.storage.exprs import get_expr


def _scope(block_hash) -> dict:
    """Get scope dict for get_expr keyword.

    Args:
        block_hash: Block hash for scope.

    Returns:
        dict: Description of the return value.
    """
    return {} if block_hash is None else {"block_hash": block_hash}


def read_bloom_node(
    astreum_node,
    expr_id: bytes,
    *,
    block=None,
    contact=None,
    level: int | None = None,
) -> BloomNode | None:
    """
    Read bloom node from storage by expr_id.

    Args:
        astreum_node: Astreum node.
        expr_id: Expression ID.
        block: Block holding the expression.
        contact: Peer contact.
        level: Node level in tree.

    Returns:
        BloomNode | None: The bloom node, or None if not found.
    """
    from astreum.crypto.bloom_tree.expr import bloom_node_from_expr

    expr = get_expr(astreum_node, expr_id, contact=contact, **_scope(block))
    if expr is None:
        return None
    try:
        return bloom_node_from_expr(
            expr, astreum_node, block=block, contact=contact, level=level
        )
    except ValueError:
        return None


class BloomTree:
    """Binary bloom tree for a 1024-block chunk."""

    def __init__(self, root_hash=None, astreum_node=None, contact=None, block_hash=None):
        """Initialize bloom tree.

        Args:
            root_hash: Hash of root node.
            astreum_node: Astreum node.
            contact: Peer contact.
            block_hash: Block holding the tree.

        Returns: None
        """
        self.root: BloomNode | None = None
        # Nodes written by this tree (new or changed), by identity.
        self._touched: dict[int, BloomNode] = {}
        # RelayEndpoint of the peer holding uncommitted nodes.
        self.contact = contact
        # The block whose header holds root_hash: its provider holds the
        # nodes, and a child reached by plain hash is in the same block.
        self.block_hash = block_hash
        if root_hash and root_hash != ZERO32 and astreum_node:
            self.root = read_bloom_node(
                astreum_node, root_hash, block=block_hash, contact=contact, level=0
            )

    def insert(self, offset: int, variants: list[bytes], node=None) -> None:
        """Insert variants at offset.

        Args:
            offset: Target offset in tree.
            variants: List of variant elements.
            node: Astreum node.

        Returns: None
        """
        """Create leaf at offset with start_hash=None, insert variants along path."""
        if self.root is None:
            self.root = BloomNode(level=0)

        self._walk(self.root, offset, 0, 1024, variants, node, level=0)

    def _walk(self, node: BloomNode, offset: int, lo: int, hi: int,
              variants: list[bytes], astreum_node, level: int) -> None:
        """Walk tree and insert variants.

        Args:
            node: Current bloom node.
            offset: Target offset.
            lo: Lower bound.
            hi: Upper bound.
            variants: Variant elements.
            astreum_node: Astreum node.
            level: Current level.

        Returns: None
        """
        node._expr = None
        for v in variants:
            bloom_insert(node.filter, v)
        self._touched[id(node)] = node

        if node.is_leaf:
            return

        mid = (lo + hi) // 2
        child_level = level + 1
        if offset < mid:
            if node.left is None:
                node.left = self._resolve_child(node, True, astreum_node)
            self._walk(node.left, offset, lo, mid, variants, astreum_node, child_level)
        else:
            if node.right is None:
                node.right = self._resolve_child(node, False, astreum_node)
            self._walk(node.right, offset, mid, hi, variants, astreum_node, child_level)

    def _resolve_child(self, parent: BloomNode, is_left: bool, astreum_node) -> BloomNode:
        """Resolve child node from storage or create new.

        Args:
            parent: Parent bloom node.
            is_left: True for left child.
            astreum_node: Astreum node.

        Returns:
            BloomNode: Description of the return value.
        """
        """Resolve a child — create new or fetch from storage."""
        level = parent.level + 1
        child_hash = parent._left_hash if is_left else parent._right_hash
        child_block = parent._left_block if is_left else parent._right_block
        if child_hash and astreum_node:
            child = read_bloom_node(
                astreum_node, child_hash, block=child_block, contact=self.contact, level=level
            )
            if child is not None:
                return child
        return BloomNode(level=level)

    def set_leaf_start_hash(self, offset: int, block_hash: bytes, astreum_node=None) -> None:
        """Set start_hash on leaf at offset.

        Args:
            offset: Target offset.
            block_hash: Block hash to set.
            astreum_node: Astreum node.

        Returns: None
        """
        """Set start_hash on an existing leaf at offset.

        With *astreum_node*, nodes along the path that are not loaded are read
        from storage.
        """
        if self.root is None:
            return
        self._set_leaf(self.root, offset, 0, 1024, block_hash, astreum_node)

    def _set_leaf(self, node: BloomNode, offset: int, lo: int, hi: int,
                  block_hash: bytes, astreum_node=None) -> bool:
        """Recursively set leaf start_hash.

        Args:
            node: Current bloom node.
            offset: Target offset.
            lo: Lower bound.
            hi: Upper bound.
            block_hash: Block hash to set.
            astreum_node: Astreum node.

        Returns:
            bool: Description of the return value.
        """
        """True if the leaf exists (the path is then rewritten)."""
        if node.is_leaf:
            node._expr = None
            node.start_hash = block_hash
            node.filter.start_hash = block_hash
            self._touched[id(node)] = node
            return True
        mid = (lo + hi) // 2
        is_left = offset < mid
        child = node.left if is_left else node.right
        if child is None and astreum_node is not None:
            has_child = node._left_hash if is_left else node._right_hash
            if has_child:
                child = self._resolve_child(node, is_left, astreum_node)
                if is_left:
                    node.left = child
                else:
                    node.right = child
        if child is None:
            return False
        next_lo = lo if is_left else mid
        next_hi = mid if is_left else hi
        if not self._set_leaf(child, offset, next_lo, next_hi, block_hash, astreum_node):
            return False
        node._expr = None
        self._touched[id(node)] = node
        return True

    def rewrite_root(self) -> None:
        """Write the root again as a cell of this block, its children by ref.

        A block's header names the cells that are new in it by plain hash. A
        block that changes nothing in the tree would otherwise name its
        parent's root, which belongs to an older record.
        """
        if self.root is None:
            return
        self.root._expr = None
        self.root._stored = None
        self._touched[id(self.root)] = self.root

    def new_exprs(self) -> list[Expr]:
        """The cells this tree wrote: every node it created or changed."""
        return [node.expr() for node in self._touched.values()]


def bloom_search(node: BloomNode | None, element: bytes) -> list[bytes]:
    """Search the bloom tree for element.

    Args:
        node: Root bloom node.
        element: Element to search.

    Returns:
        list[bytes]: Description of the return value.
    """
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


def bloom_search_leaves(
    root_hash: bytes, element: bytes, astreum_node, block_hash: bytes | None = None
) -> list[BloomNode]:
    """Search a sealed era's bloom tree from storage and return the leaves.

    Args:
        root_hash (bytes): Era root BloomNode expr hash.
        element (bytes): Element to search.
        astreum_node (Any): Astreum P2P/storage Node.
        block_hash (bytes | None): Block holding the root.

    Returns:
        list[BloomNode]: Leaves whose filter may hold the element.
    """
    root = read_bloom_node(astreum_node, root_hash, block=block_hash, level=0)
    if root is None:
        return []

    return _search_leaves(root, element, astreum_node)


def bloom_search_storage(
    root_hash: bytes, element: bytes, astreum_node, block_hash: bytes | None = None
) -> list[bytes | None]:
    """Search a sealed era's bloom tree from storage.

    Args:
        root_hash (bytes): Era root BloomNode expr hash.
        element (bytes): Element to search.
        astreum_node (Any): Astreum P2P/storage Node.
        block_hash (bytes | None): Block holding the root.

    Returns:
        list[bytes | None]: The start_hash of each matching leaf.
    """
    return [
        leaf.start_hash
        for leaf in bloom_search_leaves(root_hash, element, astreum_node, block_hash)
    ]


def _search_leaves(
    bloom_node: BloomNode, element: bytes, astreum_node
) -> list[BloomNode]:
    """Recursively search bloom tree from storage.

    Args:
        bloom_node (BloomNode): Current bloom node.
        element (bytes): Element to search.
        astreum_node (Any): Astreum node.

    Returns:
        list[BloomNode]: Matching leaves. A leaf with start_hash None means
            "match but no block pointer".
    """
    if not bloom_test(bloom_node.filter, element):
        return []
    if bloom_node.is_leaf:
        return [bloom_node]

    results: list[BloomNode] = []
    for child_hash, child_block in (
        (bloom_node._left_hash, bloom_node._left_block),
        (bloom_node._right_hash, bloom_node._right_block),
    ):
        if child_hash:
            child = read_bloom_node(
                astreum_node, child_hash, block=child_block, level=bloom_node.level + 1
            )
            if child is not None:
                results.extend(_search_leaves(child, element, astreum_node))
    return results
