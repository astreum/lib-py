"""Walk a radix tree to a key and collect the exprs a reader needs.

A reader that looks up a key reads one node after another. A node is a list of
five items: the key length, the key, two child pointers and the value. A reader
that fetches the exprs of each node one by one needs many requests.

``walk_radix`` follows the key over local storage, as ``_find_node`` does, and
returns the exprs that the reads of every node on the way need. It makes no
network call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astreum.expression import Expr
from astreum.expression.shape import HOLD, INT, SEND, pair, parse_shape, repeat, walk_shape
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.radix.tree.get import _find_node
from astreum.storage.radix.tree.model import RadixTree

if TYPE_CHECKING:
    from astreum._node import Node

# A pointer to another node. A plain hash names the root expr of the node. A
# stored ref is a list of three: the block, the hash and the ``ref`` symbol.
# This rule sends the first three cells of the pointed expr with their heads,
# which is a whole ref and the start of a plain node.
_POINTER = pair(SEND, pair(SEND, pair(SEND, HOLD)))

# One radix node: key length, key, child 0, child 1, value.
RADIX_NODE_SHAPE = pair(
    INT,
    pair(SEND, pair(_POINTER, pair(_POINTER, pair(repeat(SEND), HOLD)))),
)
_RADIX_NODE_RULE = parse_shape(RADIX_NODE_SHAPE)


def walk_radix(
    astreum_node: "Node", root_hash: bytes, key: bytes, key_bit_offset: int
) -> list[Expr]:
    """Collect the exprs for the nodes on the way to a key.

    The walk starts at the node ``root_hash``, where ``key_bit_offset`` bits of
    the key are already matched. It stops where the key ends, the prefix does
    not match, a child is missing, or local storage does not hold a node in
    full.

    Args:
        astreum_node: A Node instance. Only its local storage is read.
        root_hash: The hash of the node to start from.
        key: The key to walk to.
        key_bit_offset: The number of key bits that earlier nodes matched.

    Returns:
        list[Expr]: Each expr once, root first. The list is empty if local
        storage does not hold the root expr, or the offset is outside the key.
    """
    if not 0 <= key_bit_offset <= len(key) * 8:
        return []
    tree = RadixTree(root_hash=root_hash)
    _find_node(tree, astreum_node, key, local=True, key_pos=key_bit_offset)

    def load(h: bytes):
        return get_expr_from_local_storage(astreum_node, h)

    found: dict[bytes, Expr] = {}
    for node_hash in dict.fromkeys([root_hash, *tree.nodes]):
        exprs, _ = walk_shape(_RADIX_NODE_RULE, node_hash, load)
        for expr in exprs:
            found.setdefault(expr.hash(), expr)
    return list(found.values())
