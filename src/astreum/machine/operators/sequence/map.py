from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators.sequence._step import _step_for


def _map_bytes(machine, fn, env, value, step):
    out = bytearray()
    cost = 0
    for i in range(len(value.value)):
        machine.meter.charge_bytes(1)
        elem = bytes_(value.value[i:i + 1])
        result = step(machine, fn, env, [elem])
        if get_expr_tag(result) != "bytes":
            raise OpError(
                f"map fn produced {get_expr_tag(result)}, expected bytes"
            )
        out.extend(result.value)
        cost += result.size()
    final = bytes_(bytes(out))
    machine.meter.charge_bytes(len(out))
    return final


def _map_str(machine, fn, env, value, step):
    chars = []
    cost = 0
    for ch in value.value:
        machine.meter.charge_bytes(len(ch.encode("utf-8")))
        elem = str_(ch)
        result = step(machine, fn, env, [elem])
        if get_expr_tag(result) != "str":
            raise OpError(
                f"map fn produced {get_expr_tag(result)}, expected str"
            )
        chars.append(result.value)
        cost += len(result.value.encode("utf-8"))
    final = str_("".join(chars))
    machine.meter.charge_bytes(cost)
    return final


def _map_link(machine, fn, env, value, step):
    elems = []
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        machine.meter.charge_bytes(current.head.size())
        result = step(machine, fn, env, [current.head])
        elems.append(result)
        if current.tail is NIL or current.tail is None:
            break
        current = current.tail
    cost = sum(e.size() for e in elems)
    machine.meter.charge_bytes(cost + len(elems))
    out = NIL
    for elem in reversed(elems):
        out = link(elem, out)
    return out


def handle_stack_map(machine, stack: List[Expr], env) -> None:
    fn = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)

    step = _step_for(fn)

    if value_tag == "bytes":
        result = _map_bytes(machine, fn, env, value, step)
    elif value_tag == "str":
        result = _map_str(machine, fn, env, value, step)
    elif value_tag == "link":
        result = _map_link(machine, fn, env, value, step)
    else:
        raise OpError(f"map of {value_tag}")

    stack.append(result)


def handle_stack_map_with_result(machine, stack, env):
    try:
        handle_stack_map(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))