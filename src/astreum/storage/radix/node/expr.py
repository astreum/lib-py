from __future__ import annotations

from astreum.expression import Expr, NIL, link, int_, bytes_
from astreum.expression.refs import ref_expr
from astreum.storage.radix.node.model import RadixNode


def _pointer(h: bytes, block: bytes | None, rest: Expr) -> Expr:
    """A list cell whose head is the cell *h*: its hash, or a ref when the cell
    is held by the record of the older block *block*."""
    if block is None:
        return Expr("link", head_hash=h, tail=rest)
    return link(ref_expr(block, h), rest)


def convert_radix_node_to_expr(node: RadixNode) -> Expr:
    """Serialize a RadixNode into an S-expression for persistent storage.

    Encodes the node's key, key length, children (child_0, child_1),
    and value into a chain of nested (link ...) expressions. A pointer to a
    cell that is older than the block writing the node is a stored ref
    (``child_0_block``, ``child_1_block``, ``value_block``).

    Args:
        node: The radix node to serialize.

    Returns:
        An Expr representing the node.
    """
    if node.value is None:
        expr = link(NIL, NIL)
    elif isinstance(node.value, bytes):
        expr = _pointer(node.value, node.value_block, NIL)
    elif node.value_block is not None:
        expr = link(ref_expr(node.value_block, node.value.hash()), NIL)
    else:
        expr = link(node.value, NIL)

    expr = _pointer(node.child_1, node.child_1_block, expr) if node.child_1 else link(NIL, expr)
    expr = _pointer(node.child_0, node.child_0_block, expr) if node.child_0 else link(NIL, expr)
    expr = link(bytes_(node.key), expr)
    expr = link(int_(node.key_len), expr)
    return expr


def get_radix_node_expr(node: RadixNode) -> Expr:
    if node._expr is None:
        node._expr = convert_radix_node_to_expr(node)
    return node._expr
