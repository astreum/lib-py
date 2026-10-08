from astreum.expression import Expr, NIL, link, str_, symbol
from astreum.machine import OpError
from astreum.consensus.transaction.storage.initial import storage_walk_fee


def handle_stack_tx_log(machine, stack, env):
    value = stack.pop()
    fee = storage_walk_fee(machine.node, machine.block, value)
    if fee is None:
        raise RuntimeError("tx.log storage cost failed")
    machine.meter.charge(fee, kind="storage")
    machine.logs.append(value)


def handle_stack_tx_log_with_result(machine, stack, env):
    try:
        handle_stack_tx_log(machine, stack, env)
        stack.append(link(NIL, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
