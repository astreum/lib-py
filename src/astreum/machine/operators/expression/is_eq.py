from typing import Any, List

from astreum.expression import Expr, NIL, bytes_, FLOAT_TAGS, _expr_to_fp64, get_expr_tag, link, str_, symbol
from astreum.machine import OpError


def expr_equal(x: Any, y: Any) -> bool:
    if x is y:
        return True
    if x is None or y is None:
        return False
    x_tag = get_expr_tag(x)
    y_tag = get_expr_tag(y)
    if x_tag == "link" and y_tag == "link":
        return expr_equal(x.head, y.head) and expr_equal(x.tail, y.tail)
    if x_tag != y_tag:
        return False
    if x_tag == "int":
        return x.value == y.value
    if x_tag == "bytes":
        return x.value == y.value
    if x_tag == "symbol":
        return x.value == y.value
    if x_tag in FLOAT_TAGS:
        return _expr_to_fp64(x) == _expr_to_fp64(y)
    return False


def handle_stack_is_eq(machine, stack: List[Expr], env) -> None:
    b = stack.pop()
    a = stack.pop()
    machine.meter.charge_bytes(min(a.size(), b.size()) + 1)
    result = expr_equal(a, b)
    stack.append(bytes_(b"\x01" if result else b"\x00"))


def handle_stack_is_eq_with_result(machine, stack, env):
    try:
        handle_stack_is_eq(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
