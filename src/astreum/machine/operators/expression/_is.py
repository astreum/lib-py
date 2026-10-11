from typing import List

from astreum.expression import Expr, bytes_, get_expr_tag, get_expr_builtin_tag
from astreum.machine import OpError


def handle_stack_is(machine, stack: List[Expr], env=None) -> None:
    if len(stack) < 2:
        raise OpError("stack underflow")
    tag_sym = stack.pop()
    val = stack.pop()
    if get_expr_builtin_tag(tag_sym) != "symbol":
        raise OpError("is requires a symbol")
    target = tag_sym.value
    match = get_expr_tag(val) == target
    machine.meter.charge_bytes(1)
    stack.append(bytes_(b"\x01" if match else b"\x00"))
