from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError


def _concat_bytes(a, b):
    result = bytes_(a.value + b.value)
    return result, result.size()


def _concat_str(a, b):
    result = str_(a.value + b.value)
    encoded = result.value.encode("utf-8")
    return result, len(encoded)


def _collect_elems(value):
    elems = []
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        elems.append(current.head)
        if current.tail is NIL or current.tail is None:
            break
        current = current.tail
    return elems


def _concat_link(a, b):
    elems = _collect_elems(a)
    elems.extend(_collect_elems(b))
    if not elems:
        return NIL, 0
    cost = 0
    result = NIL
    for elem in reversed(elems):
        result = link(elem, result)
        cost += elem.size() + 1
    return result, cost


def handle_stack_concat(machine, stack: List[Expr], env) -> None:
    b = stack.pop()
    a = stack.pop()
    a_tag = get_expr_tag(a)
    b_tag = get_expr_tag(b)

    if a_tag != b_tag:
        raise OpError(
            f"concatenation of {a_tag} and {b_tag}"
        )

    if a_tag == "bytes":
        result, cost = _concat_bytes(a, b)
    elif a_tag == "str":
        result, cost = _concat_str(a, b)
    elif a_tag == "link":
        result, cost = _concat_link(a, b)
    else:
        raise OpError(
            f"concatenation of {a_tag} and {b_tag}"
        )

    machine.meter.charge_bytes(cost)
    stack.append(result)


def handle_stack_concat_with_result(machine, stack, env):
    try:
        handle_stack_concat(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
