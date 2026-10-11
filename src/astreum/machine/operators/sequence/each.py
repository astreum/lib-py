from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators.sequence._step import _step_for


def _each_bytes(machine, fn, env, value, step):
    machine.meter.charge_bytes(len(value.value))
    for i in range(len(value.value)):
        elem = bytes_(value.value[i:i + 1])
        machine.meter.charge_bytes(1)
        step(machine, fn, env, [elem])


def _each_str(machine, fn, env, value, step):
    machine.meter.charge_bytes(len(value.value.encode("utf-8")))
    for ch in value.value:
        elem = str_(ch)
        machine.meter.charge_bytes(len(ch.encode("utf-8")))
        step(machine, fn, env, [elem])


def _each_link(machine, fn, env, value, step):
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        machine.meter.charge_bytes(current.head.size())
        step(machine, fn, env, [current.head])
        if current.tail is NIL or current.tail is None:
            break
        current = current.tail


def handle_stack_each(machine, stack: List[Expr], env) -> None:
    fn = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)

    step = _step_for(fn)

    if value_tag == "bytes":
        _each_bytes(machine, fn, env, value, step)
    elif value_tag == "str":
        _each_str(machine, fn, env, value, step)
    elif value_tag == "link":
        _each_link(machine, fn, env, value, step)
    else:
        raise OpError(f"each of {value_tag}")

    stack.append(value)


def handle_stack_each_with_result(machine, stack, env):
    try:
        handle_stack_each(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))