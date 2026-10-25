"""Walk a radix tree to a key and collect the exprs a reader needs.

A reader that looks up a key reads one node after another. A node is a list of
five items: the key length, the key, two child pointers and the value. A reader
that fetches the exprs of each node one by one needs many requests.

``walk_radix_key`` follows the key over local storage, as ``_find_node`` does,
and returns the exprs that the reads of every node on the way need.

``collect_radix_all`` reads all nodes below a node, and lists the nodes that
local storage does not hold.

Both apply a value shape to the value of each node they send. Neither makes a
network call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from astreum.expression import Expr, ZERO32
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.expression.shape import HOLD, INT, SEND, pair, parse_shape, repeat, walk_shape
from astreum.storage.exprs import get_expr_from_local_storage
from astreum.storage.radix.tree.get import _find_node
from astreum.storage.radix.node import RadixNode, get_radix_node_from_storage
from astreum.storage.radix.tree.model import RadixTree

if TYPE_CHECKING:
    from astreum._node import Node

# A pointer to another node. A plain hash names the root expr of the node. A
# stored ref is a list of three: the block, the hash and the ``ref`` symbol.
# This rule sends the first three cells of the pointed expr with their heads,
# which is a whole ref and the start of a plain node.
_POINTER = pair(SEND, pair(SEND, pair(SEND, HOLD)))

# One radix node: key length, key, child 0, child 1, value.
RADIX_NODE_SHAPE = pair(
    INT,
    pair(SEND, pair(_POINTER, pair(_POINTER, pair(repeat(SEND), HOLD)))),
)
_RADIX_NODE_RULE = parse_shape(RADIX_NODE_SHAPE)


def _value_rule(value_shape: bytes) -> Optional[tuple]:
    """Parse a value shape.

    Args:
        value_shape: The shape bytes. Empty for none.

    Returns:
        The rule tree, or None if the shape is empty.

    Raises:
        ValueError: If the shape cannot be parsed.
    """
    return parse_shape(value_shape) if value_shape else None


def _value_exprs(
    load: Callable[[bytes], Optional[Expr]],
    node: Optional[RadixNode],
    value_rule: Optional[tuple],
) -> list[Expr]:
    """Collect the exprs a value shape reaches from the value of a node.

    Args:
        load: Returns the expr for a hash from local storage, or None.
        node: The node as read from local storage, or None.
        value_rule: The rule to apply to the value, or None.

    Returns:
        The exprs the rule reaches that local storage holds. Empty if there is
        no node, no value or no rule.
    """
    if node is None or node.value is None or value_rule is None:
        return []
    exprs, _ = walk_shape(value_rule, node.value.hash(), load)
    return exprs


def walk_radix_key(
    astreum_node: "Node",
    root_hash: bytes,
    key: bytes,
    key_bit_offset: int,
    value_shape: bytes = b"",
) -> list[Expr]:
    """Collect the exprs for the nodes on the way to a key.

    The walk starts at the node ``root_hash``, where ``key_bit_offset`` bits of
    the key are already matched. It stops where the key ends, the prefix does
    not match, a child is missing, or local storage does not hold a node in
    full. If the walk ends on a node that holds the key, the value shape is
    applied to the value of that node.

    Args:
        astreum_node: A Node instance. Only its local storage is read.
        root_hash: The hash of the node to start from.
        key: The key to walk to.
        key_bit_offset: The number of key bits that earlier nodes matched.
        value_shape: The shape to apply to the value at the key. Empty for none.

    Returns:
        list[Expr]: Each expr once, root first. The list is empty if local
        storage does not hold the root expr, or the offset is outside the key.

    Raises:
        ValueError: If the value shape cannot be parsed.
    """
    value_rule = _value_rule(value_shape)
    if not 0 <= key_bit_offset <= len(key) * 8:
        return []
    tree = RadixTree(root_hash=root_hash)
    found_node = _find_node(tree, astreum_node, key, local=True, key_pos=key_bit_offset)

    def load(h: bytes):
        return get_expr_from_local_storage(astreum_node, h)

    found: dict[bytes, Expr] = {}
    for node_hash in dict.fromkeys([root_hash, *tree.nodes]):
        exprs, _ = walk_shape(_RADIX_NODE_RULE, node_hash, load)
        for expr in exprs:
            found.setdefault(expr.hash(), expr)
    for expr in _value_exprs(load, found_node, value_rule):
        found.setdefault(expr.hash(), expr)
    return list(found.values())


def collect_radix_all(
    astreum_node: "Node",
    root_hash: bytes,
    root_block: Optional[bytes],
    value_shape: bytes = b"",
    follow_refs: bool = True,
    budget: Optional[int] = None,
) -> tuple[list[Expr], list[tuple[bytes, Optional[bytes]]]]:
    """Collect the exprs of all nodes below a node, and list the nodes not held.

    The walk reads breadth first from ``root_hash`` over local storage. A node
    that local storage does not hold in full is listed in the second result,
    and the walk goes on with the other nodes. For each node held, the result
    holds its exprs, the exprs the value shape reaches from its value, and then
    the walk reads its children.

    A child pointer that is a stored ref names a node of an older block. With
    ``follow_refs`` False the walk does not enter it.

    Args:
        astreum_node: A Node instance. Only its local storage is read.
        root_hash: The hash of the node to start from.
        root_block: The block whose record holds the node. None for a node
            that no block has recorded.
        value_shape: The shape to apply to the value of each node. Empty for
            none.
        follow_refs: If False, the walk stays in the block of ``root_block``.
        budget: The most bytes of encoded exprs to collect, or None for no
            limit. The exprs of one node stay together. The walk stops before
            the first node that does not fit, and reads no more nodes. The
            start node is always collected.

    Returns:
        tuple: The exprs, each once, the start node first. Then a list of
        ``(hash, block)`` for each node that local storage does not hold in
        full. The nodes after a cut by ``budget`` are not in the list.

    Raises:
        ValueError: If the value shape cannot be parsed.
    """
    value_rule = _value_rule(value_shape)

    def load(h: bytes):
        return get_expr_from_local_storage(astreum_node, h)

    found: dict[bytes, Expr] = {}
    missing: list[tuple[bytes, Optional[bytes]]] = []
    seen = {root_hash}
    queue = [(root_hash, root_block)]
    used = 0
    index = 0
    while index < len(queue):
        node_hash, node_block = queue[index]
        index += 1
        try:
            node = get_radix_node_from_storage(
                astreum_node, node_hash, block_hash=node_block, local=True
            )
        except ValueError:
            node = None
        group = {}
        node_exprs, _ = walk_shape(_RADIX_NODE_RULE, node_hash, load)
        for expr in [*node_exprs, *_value_exprs(load, node, value_rule)]:
            if expr.hash() not in found:
                group[expr.hash()] = expr
        if budget is not None:
            size = sum(4 + len(encode_expr_to_bytes(expr)) for expr in group.values())
            if found and used + size > budget:
                break
            used += size
        found.update(group)
        if node is None:
            missing.append((node_hash, node_block))
            continue
        for child_hash, child_block in (
            (node.child_0, node.child_0_block),
            (node.child_1, node.child_1_block),
        ):
            if not child_hash or child_hash == ZERO32 or child_hash in seen:
                continue
            if not follow_refs and child_block != node_block:
                continue
            seen.add(child_hash)
            queue.append((child_hash, child_block))
    return list(found.values()), missing
