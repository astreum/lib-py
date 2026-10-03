from __future__ import annotations

from typing import Dict, List, Set, Tuple, TYPE_CHECKING

from astreum.expression import Expr, ZERO32
from astreum.storage.exprs import get_expr
from astreum.storage.radix.node import get_radix_node_from_storage
from astreum.storage.radix.tree.model import RadixTree
from astreum.storage.radix.tree.utils import _bits_from_payload, _bits_to_bytes

if TYPE_CHECKING:
    from astreum._node import Node


def get_all_from_radix_tree(tree: RadixTree, astreum_node: "Node") -> Dict[bytes, Expr]:
    """Walk the entire radix tree and return every key-value pair.

    Uses iterative DFS over the node tree, reconstructing each key from
    the concatenated bit prefixes along the path.

    Args:
        tree: The radix tree to enumerate.
        astreum_node: A Node instance for fetching radix nodes from storage.

    Returns:
        A dict mapping each key (bytes) to its Expr value.
    """
    if tree.root_hash is None or tree.root_hash == ZERO32:
        return {}

    results: Dict[bytes, Expr] = {}
    stack: List[Tuple[bytes, str]] = [(tree.root_hash, "")]
    visited: Set[bytes] = set()

    while stack:
        node_hash, prefix_bits = stack.pop()
        if not node_hash or node_hash == ZERO32 or node_hash in visited:
            continue
        visited.add(node_hash)

        if node_hash in tree.nodes:
            pat_node = tree.nodes[node_hash]
        else:
            pat_node = get_radix_node_from_storage(astreum_node, node_hash, contact=tree.contact)
            tree.nodes[node_hash] = pat_node

        node_bits = _bits_from_payload(pat_node.key, pat_node.key_len)
        combined_bits = prefix_bits + node_bits

        if pat_node.value is not None:
            key_bytes = _bits_to_bytes(combined_bits)
            val = pat_node.value
            if isinstance(val, bytes):
                results[key_bytes] = get_expr(astreum_node, val, contact=tree.contact)
            else:
                results[key_bytes] = val

        if pat_node.child_0:
            stack.append((pat_node.child_0, combined_bits + "0"))
        if pat_node.child_1:
            stack.append((pat_node.child_1, combined_bits + "1"))

    return results
