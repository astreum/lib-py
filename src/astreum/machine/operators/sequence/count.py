from typing import List

from astreum.expression import Expr, NIL, get_expr_tag, get_expr_builtin_tag, int_, link, str_, symbol
from astreum.machine import OpError


def _count_bytes(value):
    result = int_(len(value.value))
    return result, result.size()


def _count_str(value):
    result = int_(len(value.value))
    encoded = result.value.to_bytes(
        max(1, (result.value.bit_length() + 8) // 8), "little", signed=True
    )
    return result, len(encoded)


def _count_link(value):
    if value is NIL or value is None:
        return int_(0), 0
    n = 0
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        n += 1
        head_cost = current.head.size()
        if current.tail is NIL or current.tail is None:
            current = NIL
            break
        current = current.tail
    return int_(n), n


def handle_stack_count(machine, stack: List[Expr], env) -> None:
    value = stack.pop()
    value_tag = get_expr_tag(value)

    if value_tag == "bytes":
        result, cost = _count_bytes(value)
    elif value_tag == "str":
        result, cost = _count_str(value)
    elif value_tag == "link":
        result, cost = _count_link(value)
    else:
        raise OpError(f"count of {value_tag}")

    machine.meter.charge_bytes(cost)
    stack.append(result)


def handle_stack_count_with_result(machine, stack, env):
    try:
        handle_stack_count(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
