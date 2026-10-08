from __future__ import annotations

from typing import TYPE_CHECKING, List

from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.model import Transaction
from astreum.expression import Expr, NIL, bytes_, get_expr_tag, int_, link, str_, symbol
from astreum.machine import OpError
from astreum.consensus.models.receipt import STATUS_SUCCESS

if TYPE_CHECKING:
    from astreum.machine.main import Machine

def handle_stack_tx_new(machine: "Machine", stack: List[Expr], env) -> None:
    """Construct an internal (unsigned) transaction and apply its effects inline.

    Stack effect: ``(code recipient amount data -- hash|nil)``.

    Construct an internal (unsigned) transaction and apply its effects inline as
    part of the current contract call. The contract appears as the nested tx's
    sender. The value transferred debits the contract's balance. Execution +
    storage fees debit the outer tx sender. No separate Transaction/Receipt is
    recorded. Net effects are absorbed by the outer tx's consensus hashes
    (replay-safe by determinism). On success pushes the nested tx's would-be
    hash (32-byte Bytes). On failure pushes NIL.
    """
    if len(stack) < 4:
        raise OpError("stack underflow")

    data_expr = stack.pop()
    amount_expr = stack.pop()
    recipient_expr = stack.pop()
    code_expr = stack.pop()

    if get_expr_tag(code_expr) != "int":
        raise OpError("tx.new: expected Int tx code")
    if get_expr_tag(recipient_expr) != "bytes":
        raise OpError("tx.new: expected Bytes recipient")
    if get_expr_tag(amount_expr) != "int":
        raise OpError("tx.new: expected Int amount")

    try:
        code = TransactionCode(int(code_expr.value))
    except ValueError:
        raise OpError("tx.new: unknown tx code")

    amount = int(amount_expr.value)
    recipient = recipient_expr.value

    # Execution of a nested CODE_ACCOUNT_CALL is metered against the outer
    # caller's remaining budget: forward it as the nested machine's cost_limit.
    cost_limit = 0
    if code == TransactionCode.CODE_ACCOUNT_CALL:
        cost_limit = machine.meter.remaining()

    contract_account = machine.block.accounts.get_account(machine.tx.recipient, machine.node)
    current_counter = contract_account.counter
    contract_account.counter = current_counter + 1
    machine.block.accounts.set_account(machine.tx.recipient, contract_account)

    inner_tx = Transaction(
        chain_id=machine.block.chain_id,
        amount=amount,
        code=code,
        counter=current_counter,
        cost_limit=cost_limit,
        data=data_expr if data_expr is not None else NIL,
        recipient=recipient,
        sender=machine.tx.recipient,
        signature=b"",
    )
    inner_tx_hash = inner_tx.expr().hash()
    machine.meter.charge_bytes(len(inner_tx_hash))

    from astreum.consensus.transaction.apply import _apply_tx_effects

    snapshot = machine.block.snapshot()
    try:
        receipt_status, execution_fee, storage_fee, logs = _apply_tx_effects(
            machine.node,
            machine.block,
            inner_tx,
            inner_tx_hash,
            nested=True,
        )
    except Exception:
        machine.block.restore(snapshot)
        stack.append(NIL)
        return

    if receipt_status != STATUS_SUCCESS:
        machine.block.restore(snapshot)
        stack.append(NIL)
        return

    # Success: bubble nested logs into the outer receipt and charge the outer
    # meter for the work the nested call performed so the outer caller's
    # cost_limit bounds total execution.
    if logs:
        machine.logs.extend(logs)
    if execution_fee > 0:
        machine.meter.charge(execution_fee, kind="eval")
    if storage_fee > 0:
        machine.meter.charge(storage_fee, kind="storage")

    stack.append(int_(1))


def handle_stack_tx_new_with_result(machine, stack, env):
    try:
        handle_stack_tx_new(machine, stack, env)
        result = stack.pop()
        stack.append(link(result, symbol("ok")))
    except OpError as e:
        stack.append(link(str_(str(e)), symbol("err")))
    except IndexError:
        stack.append(link(str_("stack underflow"), symbol("err")))