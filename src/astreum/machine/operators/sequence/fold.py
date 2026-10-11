from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators.sequence._step import _step_for


def _fold_bytes(machine, fn, env, value, acc, step):
    if not value.value:
        return acc
    machine.meter.charge_bytes(len(value.value))
    for i in range(len(value.value)):
        elem = bytes_(value.value[i:i + 1])
        machine.meter.charge_bytes(1)
        acc = step(machine, fn, env, [acc, elem])
    return acc


def _fold_str(machine, fn, env, value, acc, step):
    if not value.value:
        return acc
    machine.meter.charge_bytes(len(value.value.encode("utf-8")))
    for ch in value.value:
        elem = str_(ch)
        machine.meter.charge_bytes(len(ch.encode("utf-8")))
        acc = step(machine, fn, env, [acc, elem])
    return acc


def _fold_link(machine, fn, env, value, acc, step):
    if value is NIL or value.head is None:
        return acc
    current = value
    while get_expr_builtin_tag(current) == "link" and current.head is not None:
        machine.meter.charge_bytes(current.head.size())
        acc = step(machine, fn, env, [acc, current.head])
        if current.tail is NIL or current.tail is None:
            break
        current = current.tail
    return acc


def handle_stack_fold(machine, stack: List[Expr], env) -> None:
    fn = stack.pop()
    acc = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)

    machine.meter.charge_bytes(acc.size())

    step = _step_for(fn)

    if value_tag == "bytes":
        acc = _fold_bytes(machine, fn, env, value, acc, step)
    elif value_tag == "str":
        acc = _fold_str(machine, fn, env, value, acc, step)
    elif value_tag == "link":
        acc = _fold_link(machine, fn, env, value, acc, step)
    else:
        raise OpError(f"fold of {value_tag}")

    stack.append(acc)


def handle_stack_fold_with_result(machine, stack, env):
    try:
        handle_stack_fold(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))