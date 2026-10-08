from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.expression import Expr, ZERO32
from astreum.storage.exprs import get_expr
from astreum.storage.radix.node import RadixNode
from astreum.storage.radix.tree.model import RadixTree
from astreum.storage.radix.tree.bit import _bit, _match_prefix
from astreum.storage.radix.tree.fetch import fetch_node_from_radix_tree

if TYPE_CHECKING:
    from astreum._node import Node


def _find_node(tree: RadixTree, astreum_node: "Node", key: bytes) -> Optional[RadixNode]:
    """Return the node holding *key*, or None.

    Each node is fetched from the block that holds it: the tree's root
    block for the root, then the block a child pointer names (a plain
    hash is in the block of the node that holds it).

    Args:
        tree: The radix tree to search.
        astreum_node: A Node instance for fetching nodes.
        key: The key to find.

    Returns:
        The RadixNode at the key, or None if not found.
    """
    if tree.root_hash is None or tree.root_hash == ZERO32:
        return None

    current = fetch_node_from_radix_tree(
        tree, astreum_node, tree.root_hash, tree.root_block
    )
    if current is None:
        return None

    key_pos = 0

    while current is not None:
        if not _match_prefix(current.key, current.key_len, key, key_pos):
            return None
        key_pos += current.key_len

        if key_pos == len(key) * 8:
            return current

        try:
            next_bit = _bit(key, key_pos)
        except IndexError:
            return None

        if next_bit:
            child_hash, child_block = current.child_1, current.child_1_block
        else:
            child_hash, child_block = current.child_0, current.child_0_block
        if child_hash is None:
            return None

        current = fetch_node_from_radix_tree(tree, astreum_node, child_hash, child_block)
        if current is None:
            return None

        key_pos += 1

    return None


def exists_in_radix_tree(tree: RadixTree, astreum_node: "Node", key: bytes) -> bool:
    """Return True if *key* resolves to a leaf value in the radix tree.

    Walks the same trie path as ``get_from_radix_tree`` but stops at the leaf
    without materializing the stored value (no ``get_expr`` on the leaf), so
    checking existence does not trigger storage reads for the value.

    Args:
        tree: The radix tree to search.
        astreum_node: A Node instance for fetching nodes.
        key: The key to check.

    Returns:
        True if the key exists with a value, False otherwise.
    """
    found = _find_node(tree, astreum_node, key)
    return found is not None and found.value is not None


def get_from_radix_tree_with_block(
    tree: RadixTree, astreum_node: "Node", key: bytes
) -> tuple[Optional[Expr], Optional[bytes]]:
    """Return ``(value, block)`` for *key*.

    Returns the value and the block whose record holds it (None if the
    value is new, or the tree is not tied to a block). The cells below
    a value held by a plain hash are in that block.

    Args:
        tree: The radix tree to search.
        astreum_node: A Node instance for fetching nodes.
        key: The key to retrieve.

    Returns:
        A tuple of (Expr, block_hash), or (None, None) if not found.
    """
    found = _find_node(tree, astreum_node, key)
    if found is None or found.value is None:
        return None, None
    val = found.value
    if isinstance(val, bytes):
        scope = {} if found.value_block is None else {"block_hash": found.value_block}
        return get_expr(astreum_node, val, contact=tree.contact, **scope), found.value_block
    return val, found.value_block


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
    return get_from_radix_tree_with_block(tree, astreum_node, key)[0]
