from astreum.machine import OpError

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.storage.radix import put_in_radix_tree
from astreum.expression import Expr, NIL, ZERO32, get_expr_tag, link, str_, symbol
from astreum.consensus.constants import STORAGE_ADDRESS


def handle_stack_acc_put(machine, stack, env):
    value_expr = stack.pop()
    key_expr = stack.pop()
    if get_expr_tag(key_expr) != "bytes" or get_expr_tag(value_expr) != "bytes":
        raise RuntimeError("acc.put: expected Bytes")
    key = key_expr.value
    value = value_expr.value

    expression_account = machine.block.accounts.get_account(machine.tx.recipient, machine.node)
    if expression_account is None:
        raise RuntimeError("acc.put: expression account not found")

    sender_account = machine.block.accounts.get_account(machine.tx.sender, machine.node)
    if sender_account is None:
        raise RuntimeError("acc.put: sender account not found")
    storage_account = machine.block.accounts.get_account(STORAGE_ADDRESS, machine.node)

    storage_fee = calculate_storage_fee(machine.block, len(key) + len(value))
    if sender_account.balance < storage_fee:
        raise RuntimeError("acc.put: sender cannot afford storage fee")

    sender_account.balance -= storage_fee
    storage_account.balance += storage_fee

    put_in_radix_tree(expression_account.data, machine.node, key, value_expr)
    expression_account.data_hash = expression_account.data.root_hash or ZERO32

    machine.block.accounts.set_account(machine.tx.recipient, expression_account)
    machine.block.accounts.set_account(machine.tx.sender, sender_account)
    machine.block.accounts.set_account(STORAGE_ADDRESS, storage_account)

    machine.meter.charge_bytes(len(key) + len(value))


def handle_stack_acc_put_with_result(machine, stack, env):
    try:
        handle_stack_acc_put(machine, stack, env)
        stack.append(link(NIL, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))