from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from astreum.expression import Expr, NIL, ZERO32, get_expr_tag, get_expr_value
from astreum.storage.pointers import read_pointer
from astreum.storage.exprs import get_expr, get_expr_list
from astreum.storage.radix.node.model import RadixNode

if TYPE_CHECKING:
    from astreum._node import Node


def get_radix_node_from_storage(
    astreum_node: "Node",
    head_hash: bytes,
    contact=None,
    block_hash=None,
    local: bool = False,
) -> RadixNode:
    """Deserialize a RadixNode from its S-expression stored at the given hash.

    Fetches and resolves the 5-element expression chain
    (key_len, key, child_0, child_1, value) and reconstructs a
    RadixNode from it.

    Args:
        astreum_node: A Node instance for fetching expressions from storage.
        head_hash: The content hash of the serialized node.
        contact: Optional ``RelayEndpoint`` for network fetches.
        block_hash: The block whose record holds this node. Its cells are
            fetched from that block's provider, and a child or value held by a
            plain hash is in the same block. A child or value written as a
            stored ref names its own block.
        local: If True, no read uses the network. An expr that is not in
            local storage raises ``ValueError``.

    Returns:
        The deserialized RadixNode.

    Raises:
        ValueError: If the expression is missing, malformed, or unresolvable.
    """
    from astreum.expression import resolve_list_exprs

    if head_hash == ZERO32:
        raise ValueError("empty expr chain for Radix node")

    scope = {} if block_hash is None else {"block_hash": block_hash}
    if local:
        scope["local"] = True
    expr = get_expr_list(astreum_node, head_hash, contact=contact, **scope)
    if expr is None:
        raise ValueError("could not retrieve Radix node expr from storage")

    elements, missed = resolve_list_exprs(astreum_node, expr, contact=contact, **scope)
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
    key_len = get_expr_value(
        key_len_expr, astreum_node, contact=contact, block_hash=block_hash, local=local
    )

    if get_expr_tag(key_expr) != "bytes":
        raise ValueError("Radix node key must be Bytes")
    key = get_expr_value(
        key_expr, astreum_node, contact=contact, block_hash=block_hash, local=local
    )

    child_0: Optional[bytes] = None
    child_0_block: Optional[bytes] = None
    if child_0_expr is not NIL:
        child_0, child_0_block = read_pointer(
            astreum_node, child_0_expr, block_hash, contact, local
        )

    child_1: Optional[bytes] = None
    child_1_block: Optional[bytes] = None
    if child_1_expr is not NIL:
        child_1, child_1_block = read_pointer(
            astreum_node, child_1_expr, block_hash, contact, local
        )

    value: Optional[Expr] = None
    value_block: Optional[bytes] = None
    if value_expr is not NIL:
        value_id, value_block = read_pointer(
            astreum_node, value_expr, block_hash, contact, local
        )
        if value_id == value_expr.hash():
            value = value_expr
        else:
            value_scope = {} if value_block is None else {"block_hash": value_block}
            if local:
                value_scope["local"] = True
            value = get_expr(astreum_node, value_id, contact=contact, **value_scope)
            if value is None:
                raise ValueError("could not retrieve Radix node value from storage")

    radix_node = RadixNode(
        key_len=key_len,
        key=key,
        value=value,
        child_0=child_0,
        child_1=child_1,
        child_0_block=child_0_block,
        child_1_block=child_1_block,
        value_block=value_block,
    )
    # As stored: writing the node again with refs for its pointers would give
    # another cell until it is changed.
    radix_node._hash = head_hash
    radix_node._expr = expr
    return radix_node
