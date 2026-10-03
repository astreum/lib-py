from __future__ import annotations

from typing import Any, Tuple

from astreum.expression import Expr, NIL, ZERO32, link
from astreum.machine.meter import MeterExceededError
from astreum.machine.main import Machine
from astreum.storage.exprs import get_expr
from astreum.consensus.transaction.storage.pending import (
    add_pending_storage_contract,
    remove_pending_storage_contract,
)

from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS


def handle_expression_account_call(
    node: Any,
    block: Any,
    transaction: Any,
) -> Tuple[int, int, list]:
    # Nested CODE_ACCOUNT_CALL via tx.new creates its own machine with a meter
    # limit drawn from the outer machine's remaining budget. The outer machine
    # (the one driving this contract's evaluation) is blocked while the inner
    # call runs, so the nested machine has no outer-meter back-charge here.
    machine = Machine(
        node=node,
        mode="deterministic",
        meter_limit=int(transaction.cost_limit),
    )
    machine.tx = transaction
    machine.block = block

    expression_account = block.accounts.get_account(transaction.recipient, node)
    if expression_account is None or expression_account.code_hash == ZERO32:
        return STATUS_FAILED, machine.meter.eval, machine.meter.storage, []

    program_expr = get_expr(node, expression_account.code_hash, contact=block.contact)
    if program_expr is None:
        return STATUS_FAILED, machine.meter.eval, machine.meter.storage, []

    # Snapshot BEFORE crediting: on failure the credit reverts too (matching the
    # historical semantics where machine.accounts was not flushed on revert).
    snapshot = block.snapshot()

    # Credit the incoming amount onto the cached contract account. We mutate the
    # cached object in place — the snapshot captures its pre-call state so the
    # change reverts if evaluation fails.
    expression_account.balance += int(transaction.amount)
    block.accounts.set_account(transaction.recipient, expression_account)

    try:
        data_expr = transaction.data if transaction.data is not None else NIL
        machine.run(link(data_expr, program_expr))
    except (MeterExceededError, RuntimeError):
        block.restore(snapshot)
        logs = machine.logs
        return STATUS_FAILED, machine.meter.eval, machine.meter.storage, logs

    # Success: snapshot is discarded — block already holds the live mutated
    # state. Remove log storage contracts (finalised into the receipt).
    logs = machine.logs
    for entry in machine.log_contract_entries:
        remove_pending_storage_contract(block, entry)
    return STATUS_SUCCESS, machine.meter.eval, machine.meter.storage, logs