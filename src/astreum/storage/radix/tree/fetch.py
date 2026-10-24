from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.storage.exprs import get_expr, get_radix_from_network
from astreum.storage.radix.node import RadixNode, get_radix_node_from_storage
from astreum.storage.radix.tree.model import RadixTree

if TYPE_CHECKING:
    from astreum._node import Node


def fetch_node_from_radix_tree(
    tree: RadixTree,
    astreum_node: "Node",
    h: bytes,
    block: Optional[bytes] = None,
    key: Optional[bytes] = None,
    key_pos: int = 0,
    local: bool = False,
) -> Optional[RadixNode]:
    """Return the node with hash *h* from cache or fetched from provider.

    The *block* is the block whose record holds the node. None routes by hash.

    A reader that looks up *key* passes it with *key_pos*, the number of key
    bits that earlier nodes matched. If the node is not in local storage, one
    RADIX request then fetches it with the nodes on the way to the key, and the
    reads that follow find them in local storage. A node that does not arrive
    that way is fetched as without a key.

    With *local*, nothing uses the network. A node that local storage does not
    hold in full gives None.
    """
    cached = tree.nodes.get(h)
    if cached is not None:
        return cached

    if local:
        if get_expr(astreum_node, h, local=True) is None:
            return None
        try:
            pat_node = get_radix_node_from_storage(astreum_node, h, block_hash=block, local=True)
        except ValueError:
            return None
        tree.nodes[h] = pat_node
        return pat_node

    scope = {} if block is None else {"block_hash": block}
    if key is not None and len(key) <= astreum_node.config.storage_radix_key_max_bytes:
        get_radix_from_network(
            astreum_node, h, key, key_pos, contact=tree.contact, **scope
        )
    if get_expr(astreum_node, h, contact=tree.contact, **scope) is None:
        return None

    pat_node = get_radix_node_from_storage(
        astreum_node, h, contact=tree.contact, block_hash=block
    )
    tree.nodes[h] = pat_node
    return pat_node
