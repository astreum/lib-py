from astreum.expression import Expr, NIL, bytes_, get_expr_tag, link, str_, symbol
from astreum.machine import OpError
from astreum.storage.radix import get_from_radix_tree


def handle_stack_acc_get(machine, stack, env):
    """Get a value from account storage by key.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    key_expr = stack.pop()
    if get_expr_tag(key_expr) != "bytes":
        raise RuntimeError("acc.get: expected Bytes key")
    key = key_expr.value

    expression_account = machine.block.accounts.get_account(machine.tx.recipient, machine.node)
    if expression_account is None:
        raise RuntimeError("acc.get: expression account not found")

    value = get_from_radix_tree(expression_account.data, machine.node, key)
    if value is None:
        stack.append(NIL)
    else:
        stack.append(bytes_(value))

    machine.meter.charge_bytes(len(key))


def handle_stack_acc_get_with_result(machine, stack, env):
    """Get a value from account storage with result wrapping.
    
    Args:
        machine: The machine instance.
        stack: The evaluation stack.
        env: The environment.
    """
    try:
        handle_stack_acc_get(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))