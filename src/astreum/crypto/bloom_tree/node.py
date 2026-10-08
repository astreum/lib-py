from __future__ import annotations

from astreum.crypto.bloom_filter import BloomFilter


class BloomNode:
    """A node in the binary bloom tree. Wraps a BloomFilter and holds
    in-memory child references. Children are not serialized — they are
    fetched from storage by deterministic key.

    Level is encoded in the key (bloom:chunk:{idx}:{level}:{index}).
    Width = 1024 >> level.  Leaf = level 10 (width 1).
    start_hash is only set for leaves (the block hash returned by search)."""

    def __init__(self, level: int, start_hash: bytes | None = None):
        """Initialize bloom node.

        Args:
            level: Node level in tree.
            start_hash: Block hash for leaf nodes.

        Returns: None
        """
        self.level = level
        self.start_hash = start_hash
        self.filter = BloomFilter()
        if start_hash is not None:
            self.filter.start_hash = start_hash
        self.left: BloomNode | None = None
        self.right: BloomNode | None = None
        self._left_hash: bytes | None = None
        self._right_hash: bytes | None = None
        # The block whose record holds each child cell, when the cell is older
        # than the block writing this node (the child is written as a ref).
        self._left_block: bytes | None = None
        self._right_block: bytes | None = None
        # ``(expr_id, block)`` of the cell this node was loaded from, so an
        # unchanged node is written as the same cell, by a ref to its block.
        self._stored: tuple[bytes, bytes | None] | None = None
        self._expr = None  # cached expr

    @property
    def width(self) -> int:
        return 1024 >> self.level

    @property
    def is_leaf(self) -> bool:
        return self.level == 10

    def expr(self):
        if self._expr is not None:
            return self._expr
        self._expr = self.to_expr()
        return self._expr

    def to_expr(self):
        from astreum.crypto.bloom_tree.expr import bloom_node_to_expr
        return bloom_node_to_expr(self)

    @classmethod
    def from_expr(cls, expr, *, node_get=None):
        """Deserialize from expression.

        Args:
            expr: Expression to deserialize.
            node_get: Node getter function.

        Returns: None
        """
        from astreum.crypto.bloom_tree.expr import bloom_node_from_expr
        return bloom_node_from_expr(expr)
