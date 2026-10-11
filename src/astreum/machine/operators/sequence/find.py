from typing import List

from astreum.expression import Expr, NIL, bytes_, get_expr_tag, get_expr_builtin_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.machine.operators._if import is_truthy
from astreum.machine.operators.sequence._step import _step_for


def handle_stack_find(machine, stack: List[Expr], env) -> None:
    fn = stack.pop()
    value = stack.pop()
    value_tag = get_expr_tag(value)

    def make_not_found():
        return link(str_("not found"), symbol("none"))

    if value is NIL:
        stack.append(make_not_found())
        return

    step = _step_for(fn)

    if value_tag == "bytes":
        for i in range(len(value.value)):
            machine.meter.charge_bytes(1)
            elem = bytes_(value.value[i:i + 1])
            ok = step(machine, fn, env, [elem])
            if is_truthy(ok):
                machine.meter.charge_bytes(elem.size())
                stack.append(link(elem, symbol("some")))
                return
        stack.append(make_not_found())
        return

    if value_tag == "str":
        for ch in value.value:
            elem = str_(ch)
            machine.meter.charge_bytes(len(ch.encode("utf-8")))
            ok = step(machine, fn, env, [elem])
            if is_truthy(ok):
                machine.meter.charge_bytes(elem.size())
                stack.append(link(elem, symbol("some")))
                return
        stack.append(make_not_found())
        return

    if value_tag == "link":
        current = value
        while get_expr_builtin_tag(current) == "link" and current.head is not None:
            machine.meter.charge_bytes(current.head.size())
            ok = step(machine, fn, env, [current.head])
            if is_truthy(ok):
                machine.meter.charge_bytes(current.head.size())
                stack.append(link(current.head, symbol("some")))
                return
            if current.tail is NIL or current.tail is None:
                break
            current = current.tail
        stack.append(make_not_found())
        return

    raise OpError(f"find of {value_tag}")


def handle_stack_find_with_result(machine, stack, env):
    try:
        handle_stack_find(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))