from typing import List

from astreum.expression import Expr, NIL, ZERO32, link, bytes_, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators.expression._scoped import (
    check_block,
    fetch,
    follow_stored_ref,
    pop_scoped_args,
)
from astreum.expression.helpers import get_expr_builtin_tag


def _ref_thunk(block_id: bytes, h: bytes) -> Expr:
    """``(block_id h ref)``: evaluating it reads *h* from *block_id*."""
    return link(bytes_(block_id), link(bytes_(h), symbol("ref")))


def handle_stack_ref(machine, stack: List[Expr], env) -> None:
    """``(block_id expr_id ref)``: read one cell of *block_id*'s provider.

    A link comes back as thunks for its two children, which carry the same
    block. If the cell is itself a stored ref it is followed one level, and the
    thunks carry the block it names.
    """
    block_id, expr_id = pop_scoped_args(machine, stack, "ref")

    if expr_id == ZERO32:
        machine.meter.charge_bytes(1)
        stack.append(NIL)
        return

    if machine.node is None:
        raise OpError("ref requires a node connection")

    check_block(machine, "ref", block_id)
    resolved = fetch(machine, "ref", block_id, expr_id)

    if get_expr_builtin_tag(resolved) == "link":
        if (resolved.head is None and resolved.tail is None
                and resolved.head_hash is None and resolved.tail_hash is None):
            raise OpError("ref: expression not found")

        block_id, resolved = follow_stored_ref(machine, "ref", block_id, resolved)

    if get_expr_builtin_tag(resolved) == "link":
        if (resolved.head is None and resolved.tail is None
                and resolved.head_hash is None and resolved.tail_hash is None):
            stack.append(NIL)
            return

        head_h = resolved.head_hash
        if head_h is None:
            head_h = resolved.head.hash() if resolved.head is not None else ZERO32

        tail_h = resolved.tail_hash
        if tail_h is None:
            tail_h = resolved.tail.hash() if resolved.tail is not None else ZERO32

        machine.meter.charge_bytes(70)
        stack.append(link(_ref_thunk(block_id, head_h), _ref_thunk(block_id, tail_h)))
    else:
        machine.meter.charge_bytes(resolved.size())
        stack.append(resolved)


def handle_stack_ref_with_result(machine, stack, env):
    try:
        handle_stack_ref(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
