"""Shapes: a request that says how far a server walks below an expr.

A shape is a byte string made of rules. A rule applies to one expr, named by
its hash.

====  ==========  ====================================================
Byte  Name        Meaning
====  ==========  ====================================================
0     ``HOLD``    Do not send this expr. The requester keeps the hash.
1     ``SEND``    Send this expr. No expr behind its hashes is sent.
2     ``PAIR``    Send this expr. Apply the next rule to the expr its
                  head hash names, and the rule after it to the expr
                  its tail hash names.
3     ``REPEAT``  Send this expr. Apply the next rule to its head.
                  Repeat on its tail until the list ends.
====  ==========  ====================================================

A rule on an expr that is not a link is the same as ``SEND``. A link always
arrives with both of its hashes. A rule only decides if the exprs behind them
are also sent.
"""

from __future__ import annotations

from typing import Callable, Optional

from astreum.expression.expr import (
    BUILTIN_COMPOSITE_TYPE_NAMES,
    Expr,
    ZERO32,
    _BUILTIN_TYPE_HASH,
    get_expr_head_hash,
    get_expr_tail_hash,
)

SHAPE_HOLD = 0
SHAPE_SEND = 1
SHAPE_PAIR = 2
SHAPE_REPEAT = 3

MAX_SHAPE_DEPTH = 128

HOLD = bytes([SHAPE_HOLD])
SEND = bytes([SHAPE_SEND])
# An int: the int expr and its payload bytes expr. The ``int`` symbol is known
# and never sent.
INT = bytes([SHAPE_PAIR, SHAPE_SEND, SHAPE_HOLD])


def pair(head_rule: bytes, tail_rule: bytes) -> bytes:
    """Build a ``PAIR`` rule.

    Args:
        head_rule: The rule for the head of the expr.
        tail_rule: The rule for the tail of the expr.

    Returns:
        bytes: The encoded rule.
    """
    return bytes([SHAPE_PAIR]) + head_rule + tail_rule


def repeat(head_rule: bytes) -> bytes:
    """Build a ``REPEAT`` rule.

    Args:
        head_rule: The rule for the head of every expr along the tail chain.

    Returns:
        bytes: The encoded rule.
    """
    return bytes([SHAPE_REPEAT]) + head_rule


def parse_shape(data: bytes) -> tuple:
    """Parse and check a shape.

    Args:
        data: The encoded shape.

    Returns:
        tuple: The rule tree. ``(SHAPE_HOLD,)``, ``(SHAPE_SEND,)``,
        ``(SHAPE_PAIR, head, tail)`` or ``(SHAPE_REPEAT, head)``.

    Raises:
        ValueError: If the shape is empty, has an unknown byte, is cut
            short, has bytes left over, or is deeper than
            ``MAX_SHAPE_DEPTH``.
    """
    if not data:
        raise ValueError("shape is empty")
    pos = 0

    def read(depth: int) -> tuple:
        nonlocal pos
        if depth > MAX_SHAPE_DEPTH:
            raise ValueError("shape is too deep")
        if pos >= len(data):
            raise ValueError("shape is cut short")
        op = data[pos]
        pos += 1
        if op in (SHAPE_HOLD, SHAPE_SEND):
            return (op,)
        if op == SHAPE_PAIR:
            head = read(depth + 1)
            return (op, head, read(depth + 1))
        if op == SHAPE_REPEAT:
            return (op, read(depth + 1))
        raise ValueError(f"unknown shape byte {op}")

    rule = read(1)
    if pos != len(data):
        raise ValueError("shape has bytes left over")
    return rule


def walk_shape(
    rule: tuple,
    root_hash: bytes,
    load: Callable[[bytes], Optional[Expr]],
) -> tuple[list[Expr], list[bytes]]:
    """Walk a shape from a root hash.

    The walk sends each expr once, root first. A hash that ``load`` cannot
    return is reported as missing, and the walk does not go below it. The zero
    hash and the hash of a builtin type symbol are skipped.

    Args:
        rule: A rule tree from :func:`parse_shape`.
        root_hash: The hash of the expr to start from.
        load: Returns the expr for a hash, or None.

    Returns:
        tuple[list[Expr], list[bytes]]: The exprs the shape reaches, and the
        hashes it reaches that ``load`` could not return.
    """
    found: list[Expr] = []
    missing: list[bytes] = []
    seen: set[bytes] = set()
    missed: set[bytes] = set()
    stack: list[tuple[tuple, bytes]] = [(rule, root_hash)]
    while stack:
        current, expr_hash = stack.pop()
        if current[0] == SHAPE_HOLD:
            continue
        if not expr_hash or expr_hash == ZERO32 or _BUILTIN_TYPE_HASH.get(expr_hash) in BUILTIN_COMPOSITE_TYPE_NAMES:
            continue
        expr = load(expr_hash)
        if expr is None:
            if expr_hash not in missed:
                missed.add(expr_hash)
                missing.append(expr_hash)
            continue
        if expr_hash not in seen:
            seen.add(expr_hash)
            found.append(expr)
        if current[0] == SHAPE_SEND or expr.base != "link":
            continue
        tail_hash = get_expr_tail_hash(expr)
        head_hash = get_expr_head_hash(expr)
        if current[0] == SHAPE_PAIR:
            stack.append((current[2], tail_hash))
            stack.append((current[1], head_hash))
        else:
            stack.append((current, tail_hash))
            stack.append((current[1], head_hash))
    return found, missing
