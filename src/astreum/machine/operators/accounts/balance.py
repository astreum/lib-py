from astreum.expression import Expr, NIL, int_, link, str_, symbol
from astreum.machine import OpError


def handle_stack_acc_balance(machine, stack, env):
    """Get the balance of the expression account.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    expression_account = machine.block.accounts.get_account(machine.tx.recipient, machine.node)
    if expression_account is None:
        raise RuntimeError("acc.balance: expression account not found")
    stack.append(int_(expression_account.balance))
    machine.meter.charge_bytes(1)


def handle_stack_acc_balance_with_result(machine, stack, env):
    """Get account balance with result wrapping.

    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.

    Returns: None
    """
    try:
        handle_stack_acc_balance(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))
