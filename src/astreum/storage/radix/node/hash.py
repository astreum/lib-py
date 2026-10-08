from __future__ import annotations

from astreum.storage.radix.node.model import RadixNode


def radix_node_hash(node: RadixNode) -> bytes:
    """Return the content hash of a radix node.

    Args:
        node: The radix node to hash.

    Returns:
        The 32-byte content hash.
    """
    if node._hash is None:
        from astreum.storage.radix.node.expr import convert_radix_node_to_expr
        node._hash = convert_radix_node_to_expr(node).hash()
    return node._hash


def invalidate_radix_node_cache(node: RadixNode) -> None:
    """Clear the cached hash and expression for a node.

    Args:
        node: The radix node to invalidate.

    Returns: None
    """
    node._hash = None
    node._expr = None
