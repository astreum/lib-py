from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.storage.exprs import get_expr
from astreum.storage.radix.node import RadixNode, get_radix_node_from_storage
from astreum.storage.radix.tree.model import RadixTree

if TYPE_CHECKING:
    from astreum._node import Node


def fetch_node_from_radix_tree(tree: RadixTree, astreum_node: "Node", h: bytes) -> Optional[RadixNode]:
    cached = tree.nodes.get(h)
    if cached is not None:
        return cached

    if get_expr(astreum_node, h, contact=tree.contact) is None:
        return None

    pat_node = get_radix_node_from_storage(astreum_node, h, contact=tree.contact)
    tree.nodes[h] = pat_node
    return pat_node
