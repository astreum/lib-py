from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.storage.exprs import get_expr
from astreum.storage.radix.node import RadixNode, get_radix_node_from_storage
from astreum.storage.radix.tree.model import RadixTree

if TYPE_CHECKING:
    from astreum._node import Node


def fetch_node_from_radix_tree(
    tree: RadixTree, astreum_node: "Node", h: bytes, block: Optional[bytes] = None
) -> Optional[RadixNode]:
    """The node with hash *h*: cached, or fetched from the provider of *block*
    (the block whose record holds it; None routes by the hash)."""
    cached = tree.nodes.get(h)
    if cached is not None:
        return cached

    scope = {} if block is None else {"block_hash": block}
    if get_expr(astreum_node, h, contact=tree.contact, **scope) is None:
        return None

    pat_node = get_radix_node_from_storage(
        astreum_node, h, contact=tree.contact, block_hash=block
    )
    tree.nodes[h] = pat_node
    return pat_node
