from __future__ import annotations

from typing import List, Optional, Tuple, TYPE_CHECKING, Union

from astreum.expression import Expr, ZERO32
from astreum.storage.radix.node import RadixNode, radix_node_hash, invalidate_radix_node_cache
from astreum.storage.radix.tree.model import RadixTree
from astreum.storage.radix.tree.bit import _bit, _match_prefix, _bit_slice
from astreum.storage.radix.tree.common import _make_node
from astreum.storage.radix.tree.fetch import fetch_node_from_radix_tree

if TYPE_CHECKING:
    from astreum._node import Node


def _value_hash(value: Union[Expr, bytes, None]) -> bytes | None:
    """Return the hash of a value, or the value itself if already a hash.

    Args:
        value: An Expr or bytes hash, or None.

    Returns:
        The 32-byte hash, or None.
    """
    if value is None or isinstance(value, bytes):
        return value
    return value.hash()


def put_in_radix_tree(tree: RadixTree, astreum_node: "Node", key: bytes, value: Union[Expr, bytes]) -> None:
    """Write *value* at *key*, rewriting the path from the root to it.

    Every node written is new. A pointer from a written node to one that is
    not written keeps the block that holds it (``child_*_block``,
    ``value_block``), and is encoded as a stored ref to that block. A write
    that does not change the value leaves the tree as it is.

    Args:
        tree: The radix tree to update.
        astreum_node: A Node instance for fetching nodes.
        key: The key to write at.
        value: The value to write (Expr or bytes hash).

    Returns: None
    """
    total_bits = len(key) * 8

    if tree.root_hash is None or tree.root_hash == ZERO32:
        leaf = _make_node(key, total_bits, value, None, None)
        leaf_hash = radix_node_hash(leaf)
        tree.nodes[leaf_hash] = leaf
        tree.root_hash = leaf_hash
        tree.root_block = None
        return

    stack: List[Tuple[RadixNode, bytes, int]] = []
    current = fetch_node_from_radix_tree(
        tree, astreum_node, tree.root_hash, tree.root_block
    )
    assert current is not None
    key_pos = 0

    while True:
        if not _match_prefix(current.key, current.key_len, key, key_pos):
            _split_and_insert(tree, current, stack, key, key_pos, value)
            return

        key_pos += current.key_len

        if key_pos == total_bits:
            if current.value is not None and _value_hash(current.value) == _value_hash(value):
                return  # the same value: nothing is written
            old_hash = radix_node_hash(current)
            current.value = value
            current.value_block = None
            invalidate_radix_node_cache(current)
            new_hash = radix_node_hash(current)
            if new_hash != old_hash:
                tree.nodes.pop(old_hash, None)
            tree.nodes[new_hash] = current
            _bubble(tree, stack, new_hash)
            return

        next_bit = _bit(key, key_pos)
        if next_bit:
            child_hash, child_block = current.child_1, current.child_1_block
        else:
            child_hash, child_block = current.child_0, current.child_0_block

        if child_hash is None:
            _append_leaf(tree, current, next_bit, key, key_pos, value, stack)
            return

        stack.append((current, radix_node_hash(current), int(next_bit)))

        child = fetch_node_from_radix_tree(tree, astreum_node, child_hash, child_block)
        if child is None:
            parent, _, _ = stack[-1]
            _append_leaf(tree, parent, next_bit, key, key_pos, value, stack[:-1])
            return

        current = child
        key_pos += 1


def _append_leaf(
    tree: RadixTree,
    parent: RadixNode,
    dir_bit: bool,
    key: bytes,
    key_pos: int,
    value: Union[Expr, bytes],
    stack: List[Tuple[RadixNode, bytes, int]],
) -> None:
    """Append a new leaf node under a parent.

    Args:
        tree: The radix tree to update.
        parent: The parent node to attach to.
        dir_bit: Direction (0 or 1) for the child pointer.
        key: The full key being inserted.
        key_pos: Current bit position in the key.
        value: The value to store.
        stack: Ancestor stack for bubbling updates.

    Returns:
        None
    """
    tail_len = len(key) * 8 - (key_pos + 1)
    tail_bits, tail_len = _bit_slice(key, key_pos + 1, tail_len)
    leaf = _make_node(tail_bits, tail_len, value, None, None)
    leaf_hash = radix_node_hash(leaf)
    tree.nodes[leaf_hash] = leaf

    old_parent_hash = radix_node_hash(parent)

    if dir_bit:
        parent.child_1 = leaf_hash
        parent.child_1_block = None
    else:
        parent.child_0 = leaf_hash
        parent.child_0_block = None

    invalidate_radix_node_cache(parent)
    new_parent_hash = radix_node_hash(parent)
    if new_parent_hash != old_parent_hash:
        tree.nodes.pop(old_parent_hash, None)
    tree.nodes[new_parent_hash] = parent
    _bubble(tree, stack, new_parent_hash)


def _split_and_insert(
    tree: RadixTree,
    node: RadixNode,
    stack: List[Tuple[RadixNode, bytes, int]],
    key: bytes,
    key_pos: int,
    value: Union[Expr, bytes],
) -> None:
    """Split a node and insert a new branch.

    Args:
        tree: The radix tree to update.
        node: The node to split.
        stack: Ancestor stack for bubbling updates.
        key: The full key being inserted.
        key_pos: Current bit position in the key.
        value: The value to store.

    Returns:
        None
    """
    max_lcp = min(node.key_len, len(key) * 8 - key_pos)
    lcp = 0
    while lcp < max_lcp and _bit(node.key, lcp) == _bit(key, key_pos + lcp):
        lcp += 1

    old_div_bit = _bit(node.key, lcp)
    new_div_bit = _bit(key, key_pos + lcp)

    common_bits, common_len = _bit_slice(node.key, 0, lcp)
    internal = _make_node(common_bits, common_len, None, None, None)

    old_suffix_bits, old_suffix_len = _bit_slice(
        node.key,
        lcp + 1,
        node.key_len - lcp - 1
    )
    old_node_hash = radix_node_hash(node)

    node.key = old_suffix_bits
    node.key_len = old_suffix_len
    invalidate_radix_node_cache(node)
    new_node_hash = radix_node_hash(node)
    if new_node_hash != old_node_hash:
        tree.nodes.pop(old_node_hash, None)
    tree.nodes[new_node_hash] = node

    new_tail_len = len(key) * 8 - (key_pos + lcp + 1)
    new_tail_bits, _ = _bit_slice(key, key_pos + lcp + 1, new_tail_len)
    leaf = _make_node(new_tail_bits, new_tail_len, value, None, None)
    leaf_hash = radix_node_hash(leaf)
    tree.nodes[leaf_hash] = leaf

    # The re-keyed node is a new cell, so both pointers are plain hashes.
    if old_div_bit:
        internal.child_1 = new_node_hash
        internal.child_0 = leaf_hash
    else:
        internal.child_0 = new_node_hash
        internal.child_1 = leaf_hash

    invalidate_radix_node_cache(internal)
    internal_hash = radix_node_hash(internal)
    tree.nodes[internal_hash] = internal

    if not stack:
        tree.root_hash = internal_hash
        tree.root_block = None
        return

    parent, old_hash, dir_bit = stack.pop()
    if dir_bit == 0:
        parent.child_0 = internal_hash
        parent.child_0_block = None
    else:
        parent.child_1 = internal_hash
        parent.child_1_block = None
    invalidate_radix_node_cache(parent)
    new_parent_hash = radix_node_hash(parent)
    if new_parent_hash != old_hash:
        tree.nodes.pop(old_hash, None)
    tree.nodes[new_parent_hash] = parent
    _bubble(tree, stack, new_parent_hash)


def _bubble(
    tree: RadixTree,
    stack: List[Tuple[RadixNode, bytes, int]],
    new_hash: bytes
) -> None:
    """Bubble a hash change up the ancestor stack.

    Args:
        tree: The radix tree to update.
        stack: Ancestor stack (parent, old_hash, dir_bit).
        new_hash: The new hash to propagate.

    Returns:
        None
    """
    while stack:
        parent, old_hash, dir_bit = stack.pop()

        if dir_bit == 0:
            parent.child_0 = new_hash
            parent.child_0_block = None
        else:
            parent.child_1 = new_hash
            parent.child_1_block = None

        invalidate_radix_node_cache(parent)
        new_hash = radix_node_hash(parent)
        if new_hash != old_hash:
            tree.nodes.pop(old_hash, None)
        tree.nodes[new_hash] = parent

    tree.root_hash = new_hash
    tree.root_block = None
