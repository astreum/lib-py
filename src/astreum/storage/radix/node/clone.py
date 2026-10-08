from __future__ import annotations

from astreum.storage.radix.node.model import RadixNode


def clone_radix_node(node: RadixNode) -> RadixNode:
    """Create a deep copy of a radix node.

    Args:
        node: The radix node to clone.

    Returns:
        A new RadixNode with the same data.
    """
    cloned = RadixNode(
        key_len=node.key_len,
        key=node.key,
        value=None if node.value is None else (
            node.value if isinstance(node.value, bytes) else node.value
        ),
        child_0=None if node.child_0 is None else node.child_0,
        child_1=None if node.child_1 is None else node.child_1,
        child_0_block=node.child_0_block,
        child_1_block=node.child_1_block,
        value_block=node.value_block,
    )
    cloned._hash = None if node._hash is None else node._hash
    cloned._expr = node._expr
    return cloned
