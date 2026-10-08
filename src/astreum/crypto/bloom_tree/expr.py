from __future__ import annotations

from astreum.expression import Expr, NIL, ZERO32, get_referenced_hash, link
from astreum.expression.expr import TAG_BYTE_DECODINGS
from astreum.expression.refs import pointer_item
from astreum.crypto.bloom_tree.node import BloomNode
from astreum.crypto.bloom_filter.main import BloomFilter
from astreum.crypto.bloom_filter.expr import bloom_to_expr as _filter_to_expr
from astreum.storage.pointers import cell_reader, read_field


def _ref(value: bytes | None) -> Expr:
    """NIL when None, Link(head_hash=value) when set."""
    return NIL if value is None else Expr("link", head_hash=value)


def _child_item(child: BloomNode | None, h: bytes | None, block: bytes | None) -> Expr:
    """The cell for a child pointer: NIL, the hash of a cell that is new, or a
    ref to the block that holds a cell that is not rewritten.

    A loaded child that is not changed is written as it was stored, so the
    pointer keeps the block it came from; a changed one is new.
    """
    if child is not None:
        h = child.expr().hash()
        stored = child._stored
        block = stored[1] if stored is not None and stored[0] == h else None
    if h is None:
        return NIL
    return pointer_item(h, block)


def bloom_node_to_expr(node: BloomNode) -> Expr:
    """Serialize a BloomNode to an Expr.

    Format: Link(block_ref, Link(left_ref, Link(right_ref, Link(bloom_filter, NIL))))
    block_ref = NIL when absent, Link(head_hash=start_hash) when set.
    A child pointer is NIL when absent, Link(head_hash=child) for a child that
    is new in this block and ``(block_id child ref)`` for one that is older
    (docs/plans/2026-10-08-refs-in-tries.md).
    """
    block_ref = _ref(node.start_hash)
    left_ref = _child_item(node.left, node._left_hash, node._left_block)
    right_ref = _child_item(node.right, node._right_hash, node._right_block)
    filter_expr = _filter_to_expr(node.filter)

    return link(
        block_ref,
        link(
            left_ref,
            link(
                right_ref,
                link(
                    filter_expr,
                    NIL,
                ),
            ),
        ),
    )


def _is_nil(cell: Expr | None) -> bool:
    return (
        cell is None
        or cell is NIL
        or (
            cell._tag == "link"
            and cell._head is None and cell._tail is None
            and cell._head_hash is None and cell._tail_hash is None
        )
    )


def _int_value(cell: Expr, part) -> int:
    if cell.value is not None:
        return cell.value
    head = part(cell, "head")
    if head is None or head._tag != "bytes":
        raise ValueError("bloom filter count is not an int")
    return TAG_BYTE_DECODINGS["int"](head.value)


def bloom_node_from_expr(
    expr: Expr,
    astreum_node=None,
    *,
    block=None,
    contact=None,
    level: int | None = None,
) -> BloomNode:
    """Deserialize a BloomNode from an Expr.

    The cells of *expr* are read as loaded; any that is only a hash is fetched
    with *astreum_node* from the provider of *block*, the block whose record
    holds *expr* (a child written as a ref names its own block). Without a
    node nothing is fetched.

    *level* is the node's depth in its tree (the root is 0); a leaf is level
    10. When not given it is 10 for a node without children, else 0.
    """
    part = cell_reader(astreum_node, block, contact)

    def head(cell):
        h = part(cell, "head") if cell is not None else None
        return None if _is_nil(h) else h

    def tail(cell):
        t = part(cell, "tail") if cell is not None else None
        return None if _is_nil(t) else t

    # Link(block_ref, Link(left_ref, Link(right_ref, Link(filter, NIL))))
    c2 = tail(expr)
    c3 = tail(c2)
    c4 = tail(c3)
    if c4 is None:
        raise ValueError("malformed bloom node")
    block_ref, left_ref, right_ref, filter_cell = head(expr), head(c2), head(c3), head(c4)
    if filter_cell is None:
        raise ValueError("malformed bloom node: no filter")

    start_hash = None if block_ref is None else get_referenced_hash(block_ref)
    left = None if left_ref is None else read_field(astreum_node, left_ref, block, contact)
    right = None if right_ref is None else read_field(astreum_node, right_ref, block, contact)

    # Link(start_hash, Link(count, tiers))
    count_cell = head(tail(filter_cell))
    if count_cell is None:
        raise ValueError("malformed bloom filter")
    bf = BloomFilter()
    bf.count = _int_value(count_cell, part)
    bf.start_hash = start_hash
    current = tail(tail(filter_cell))
    while current is not None:
        tier = head(current)
        if tier is not None:
            bf.tiers.append(bytearray(tier.value))
        current = tail(current)

    is_leaf = left is None and right is None
    node = BloomNode(level=level if level is not None else (10 if is_leaf else 0))
    node.filter = bf
    node.start_hash = start_hash
    if left is not None:
        node._left_hash, node._left_block = left
    if right is not None:
        node._right_hash, node._right_block = right
    node._stored = (expr.hash(), block)
    node._expr = expr
    return node
