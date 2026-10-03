from __future__ import annotations

from astreum.storage.radix.node import clone_radix_node
from astreum.storage.radix.tree.model import RadixTree


def clone_radix_tree(tree: RadixTree) -> RadixTree:
    cloned = RadixTree(
        root_hash=None if tree.root_hash is None else tree.root_hash,
        contact=tree.contact,
    )
    cloned.nodes = {
        node_hash: clone_radix_node(node)
        for node_hash, node in tree.nodes.items()
    }
    return cloned
