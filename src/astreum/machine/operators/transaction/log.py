from astreum.expression import Expr, NIL, link, str_, symbol
from astreum.expression.helpers import exprs_to_linked_expr
from astreum.machine import OpError
from astreum.consensus.transaction.storage.initial import storage_walk_fee


def handle_stack_tx_log(machine, stack, env):
    value = stack.pop()
    # Halt when the sender cannot pay for this log. The log is not kept. The
    # logs before it stay and are paid for. A nested call has no signature and
    # its fees go to the outer meter, so only the top-level call is checked.
    if machine.tx.signature:
        sender = machine.block.accounts.get_account(machine.tx.sender, machine.node)
        logs_fee = storage_walk_fee(
            machine.node, machine.block, exprs_to_linked_expr(machine.logs + [value])
        )
        if logs_fee is None:
            raise RuntimeError("tx.log storage cost failed")
        if 1 + machine.meter.eval + logs_fee + machine.tx.amount > sender.balance:
            raise RuntimeError("tx.log: sender balance cannot pay for the log")
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
