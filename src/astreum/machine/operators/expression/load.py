from typing import List

from astreum.expression import Expr, NIL, ZERO32, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators.expression._scoped import (
    check_block,
    fetch,
    follow_stored_ref,
    pop_scoped_args,
    stored_ref_target,
)
from astreum.storage.exprs import get_expr
from astreum.expression.helpers import get_expr_builtin_tag


def _child(machine, block_id: bytes, cell: Expr, side: str):
    """Return the resolved head or tail of *cell*.

    Returns None for nothing. Either a loaded child, or one fetched from
    *block_id*, then loaded in turn.
    """
    child = cell.head if side == "head" else cell.tail
    h = cell.head_hash if side == "head" else cell.tail_hash
    if child is None:
        if h is None or h == ZERO32:
            return None
        child = get_expr(machine.node, h, block_hash=block_id)
        if child is None:
            raise OpError("load: expression not found")
    return _resolve(machine, block_id, child)


def _resolve(machine, block_id: bytes, cell: Expr, follow: bool = True) -> Expr:
    """*cell* with everything below it loaded and every stored ref followed.

    Built as a new expr: the cell may be a shared one in storage, and following
    a ref gives it a different hash.
    """
    if get_expr_builtin_tag(cell) != "link":
        return cell
    head = _child(machine, block_id, cell, "head")
    tail = _child(machine, block_id, cell, "tail")
    rebuilt = cell
    if head is not None or tail is not None:
        rebuilt = Expr("link", head=head, tail=tail)
    if follow and stored_ref_target(machine, block_id, rebuilt) is not None:
        next_block, target = follow_stored_ref(machine, "load", block_id, rebuilt)
        # One level: the target is loaded but not itself taken for a ref.
        if get_expr_builtin_tag(target) == "link":
            return _resolve(machine, next_block, target, follow=False)
        return target
    return rebuilt


def handle_stack_load(machine, stack: List[Expr], env) -> None:
    """Load the whole expr, following all refs below it.

    ``(block_id expr_id load)``: read from *block_id*'s provider.
    """
    block_id, expr_id = pop_scoped_args(machine, stack, "load")

    if expr_id == ZERO32:
        machine.meter.charge_bytes(1)
        stack.append(NIL)
        return

    if machine.node is None:
        raise OpError("load requires a node connection")

    check_block(machine, "load", block_id)
    root = fetch(machine, "load", block_id, expr_id)
    resolved = _resolve(machine, block_id, root)

    machine.meter.charge_bytes(resolved.size() * 2)
    stack.append(resolved)


def handle_stack_load_with_result(machine, stack, env):
    try:
        handle_stack_load(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
