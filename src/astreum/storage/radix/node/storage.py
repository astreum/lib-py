from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.expression import Expr, NIL, ZERO32, get_expr_tag, get_expr_value
from astreum.storage.exprs import get_expr_list
from astreum.storage.radix.node.model import RadixNode

if TYPE_CHECKING:
    from astreum._node import Node


def get_radix_node_from_storage(
    astreum_node: "Node", head_hash: bytes, contact=None
) -> RadixNode:
    """Deserialize a RadixNode from its S-expression stored at the given hash.

    Fetches and resolves the 5-element expression chain
    (key_len, key, child_0, child_1, value) and reconstructs a
    RadixNode from it.

    Args:
        astreum_node: A Node instance for fetching expressions from storage.
        head_hash: The content hash of the serialized node.
        contact: Optional ``(relay_key, address, port)`` for network fetches.

    Returns:
        The deserialized RadixNode.

    Raises:
        ValueError: If the expression is missing, malformed, or unresolvable.
    """
    from astreum.expression import resolve_list_exprs

    if head_hash == ZERO32:
        raise ValueError("empty expr chain for Radix node")

    expr = get_expr_list(astreum_node, head_hash, contact=contact)
    if expr is None:
        raise ValueError("could not retrieve Radix node expr from storage")

    elements, missed = resolve_list_exprs(astreum_node, expr, contact=contact)
    if missed:
        raise ValueError(
            f"unresolved hashes in Radix node expr (missed={[h.hex()[:8] for h in missed]})"
        )
    if len(elements) != 5:
        raise ValueError(
            f"malformed Radix node expr length (got={len(elements)}, expected=5)"
        )

    key_len_expr, key_expr, child_0_expr, child_1_expr, value_expr = elements

    if get_expr_tag(key_len_expr) != "int":
        raise ValueError("Radix node key_len must be Int")
    key_len = get_expr_value(key_len_expr, astreum_node, contact=contact)

    if get_expr_tag(key_expr) != "bytes":
        raise ValueError("Radix node key must be Bytes")
    key = get_expr_value(key_expr, astreum_node, contact=contact)

    child_0: Optional[bytes] = None
    if child_0_expr is not NIL:
        child_0 = child_0_expr.hash()

    child_1: Optional[bytes] = None
    if child_1_expr is not NIL:
        child_1 = child_1_expr.hash()

    value: Optional[Expr] = None
    if value_expr is not NIL:
        value = value_expr

    return RadixNode(key_len=key_len, key=key, value=value, child_0=child_0, child_1=child_1)
