from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators._if import is_truthy
from astreum.machine.operators.sequence._step import _step_for


def _filter_bytes(machine, fn, env, value, step):
    out = bytearray()
    for i in range(len(value.value)):
        machine.meter.charge_bytes(1)
        elem = bytes_(value.value[i:i + 1])
        ok = step(machine, fn, env, [elem])
        if is_truthy(ok):
            out.extend(elem.value)
    machine.meter.charge_bytes(len(out))
    return bytes_(bytes(out))


def _filter_str(machine, fn, env, value, step):
    chars = []
    for ch in value.value:
        machine.meter.charge_bytes(len(ch.encode("utf-8")))
        elem = str_(ch)
        ok = step(machine, fn, env, [elem])
        if is_truthy(ok):
            chars.append(elem.value)
    joined = "".join(chars)
    machine.meter.charge_bytes(len(joined.encode("utf-8")))
    return str_(joined)


def _filter_link(machine, fn, env, value, step):
    kept = []
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        machine.meter.charge_bytes(current.head.size())
        ok = step(machine, fn, env, [current.head])
        if is_truthy(ok):
            kept.append(current.head)
        if current.tail is NIL or current.tail is None:
            break
        current = current.tail
    cost = sum(e.size() for e in kept)
    machine.meter.charge_bytes(cost + len(kept))
    out = NIL
    for elem in reversed(kept):
        out = link(elem, out)
    return out


def handle_stack_filter(machine, stack: List[Expr], env) -> None:
    fn = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)

    step = _step_for(fn)

    if value_tag == "bytes":
        result = _filter_bytes(machine, fn, env, value, step)
    elif value_tag == "str":
        result = _filter_str(machine, fn, env, value, step)
    elif value_tag == "link":
        result = _filter_link(machine, fn, env, value, step)
    else:
        raise OpError(f"filter of {value_tag}")

    stack.append(result)


def handle_stack_filter_with_result(machine, stack, env):
    try:
        handle_stack_filter(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))