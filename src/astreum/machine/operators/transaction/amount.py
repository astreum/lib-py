from astreum.expression import Expr, NIL, int_, link, str_, symbol
from astreum.expression.expr import _encode_int
from astreum.machine import OpError


def handle_stack_tx_amount(machine, stack, env):
    value = int_(machine.tx.amount)
    stack.append(value)
    machine.meter.charge_bytes(len(_encode_int(value.value)))


def handle_stack_tx_amount_with_result(machine, stack, env):
    try:
        handle_stack_tx_amount(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
