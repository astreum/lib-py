from __future__ import annotations

from typing import Optional, Union

from astreum.expression import Expr
from astreum.storage.radix.node import RadixNode


def _make_node(
    prefix_bits: bytes,
    prefix_len: int,
    value: Optional[Union[Expr, bytes]],
    child0: Optional[bytes],
    child1: Optional[bytes],
) -> RadixNode:
    """Create a new RadixNode.

    Args:
        prefix_bits: The key prefix bytes.
        prefix_len: The length of the prefix in bits.
        value: Optional value (Expr or bytes hash).
        child0: Optional hash of child node for bit 0.
        child1: Optional hash of child node for bit 1.

    Returns:
        A new RadixNode instance.
    """
    return RadixNode(prefix_len, prefix_bits, value, child0, child1)
