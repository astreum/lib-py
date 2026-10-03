from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.expression import Expr, ZERO32
from astreum.storage.exprs import get_expr
from astreum.storage.radix.tree.model import RadixTree
from astreum.storage.radix.tree.bit import _bit, _match_prefix
from astreum.storage.radix.tree.fetch import fetch_node_from_radix_tree

if TYPE_CHECKING:
    from astreum._node import Node


def exists_in_radix_tree(tree: RadixTree, astreum_node: "Node", key: bytes) -> bool:
    """Return True if *key* resolves to a leaf value in the radix tree.

    Walks the same trie path as ``get_from_radix_tree`` but stops at the leaf
    without materializing the stored value (no ``get_expr`` on the leaf), so
    checking existence does not trigger storage reads for the value.
    """
    if tree.root_hash is None or tree.root_hash == ZERO32:
        return False

    current = fetch_node_from_radix_tree(tree, astreum_node, tree.root_hash)
    if current is None:
        return False

    key_pos = 0

    while current is not None:
        if not _match_prefix(current.key, current.key_len, key, key_pos):
            return False
        key_pos += current.key_len

        if key_pos == len(key) * 8:
            return current.value is not None

        try:
            next_bit = _bit(key, key_pos)
        except IndexError:
            return False

        child_hash = current.child_1 if next_bit else current.child_0
        if child_hash is None:
            return False

        current = fetch_node_from_radix_tree(tree, astreum_node, child_hash)
        if current is None:
            return False

        key_pos += 1

    return False


def get_from_radix_tree(tree: RadixTree, astreum_node: "Node", key: bytes) -> Optional[Expr]:
    """Walk the radix tree to retrieve the value stored at the given key.

    Follows the node chain from the root, matching the key bit-by-bit
    against each node's prefix.  Returns the expression stored at the
    leaf, or None if the key is not present.

    Args:
        tree: The radix tree to search.
        astreum_node: A Node instance for fetching radix nodes from storage.
        key: The key to look up.

    Returns:
        The Expr if found, or None.
    """
    if tree.root_hash is None or tree.root_hash == ZERO32:
        return None

    current = fetch_node_from_radix_tree(tree, astreum_node, tree.root_hash)
    if current is None:
        return None

    key_pos = 0

    while current is not None:
        if not _match_prefix(current.key, current.key_len, key, key_pos):
            return None
        key_pos += current.key_len

        if key_pos == len(key) * 8:
            val = current.value
            if val is None:
                return None
            if isinstance(val, bytes):
                return get_expr(astreum_node, val, contact=tree.contact)
            return val

        try:
            next_bit = _bit(key, key_pos)
        except IndexError:
            return None

        child_hash = current.child_1 if next_bit else current.child_0
        if child_hash is None:
            return None

        current = fetch_node_from_radix_tree(tree, astreum_node, child_hash)
        if current is None:
            return None

        key_pos += 1

    return None
