from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError


def _reverse_bytes(value):
    reversed_bytes = bytes(reversed(value.value))
    result = bytes_(reversed_bytes)
    return result, result.size()


def _reverse_str(value):
    chars = list(value.value)
    reversed_chars = chars[::-1]
    result = str_("".join(reversed_chars))
    encoded = result.value.encode("utf-8")
    return result, len(encoded)


def _reverse_link(value):
    elems = []
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        head_cost = current.head.size()
        if current.tail is NIL or current.tail is None:
            elems.append(current.head)
            current = NIL
            break
        elems.append(current.head)
        current = current.tail

    cost = sum(e.size() for e in elems)
    result = NIL
    for elem in elems:
        cost += 1
        result = link(elem, result)
    return result, cost


def handle_stack_reverse(machine, stack: List[Expr], env) -> None:
    value = stack.pop()
    value_tag = get_expr_tag(value)

    if value_tag == "bytes":
        result, cost = _reverse_bytes(value)
    elif value_tag == "str":
        result, cost = _reverse_str(value)
    elif value_tag == "link":
        result, cost = _reverse_link(value)
    else:
        raise OpError(f"reverse of {value_tag}")

    machine.meter.charge_bytes(cost)
    stack.append(result)


def handle_stack_reverse_with_result(machine, stack, env):
    try:
        handle_stack_reverse(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
