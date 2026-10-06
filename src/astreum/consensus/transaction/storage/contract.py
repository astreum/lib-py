from __future__ import annotations

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.expression import NIL
from astreum.consensus.transaction.model import Transaction


def _int_be_len(value: int) -> int:
    v = value
    if v == 0:
        return 1
    return (v.bit_length() + 7) // 8


def calculate_transaction_costs(*, block: object, transaction: Transaction) -> int:
    """Estimate mandatory storage cost using the Expr.size() model."""
    tx_total_bytes = transaction.expr().size()
    tx_storage_cost = calculate_storage_fee(block, tx_total_bytes)
    total_storage_fee = tx_storage_cost
    receipt_storage_cost = 0

    tx_fee = 1
    logs_expr = NIL

    # Receipt bytes include the storage-fee field, so converge to a fixed point.
    for _ in range(8):
        receipt_bytes = (
            _int_be_len(total_storage_fee)
            + _int_be_len(tx_fee)
            + logs_expr.size()
            + 7                                       # Symbol("receipt")
            + 10 * 32                                 # link overhead (10 Links × 32)
            + 1                                       # status byte
        )
        receipt_storage_cost = calculate_storage_fee(block, receipt_bytes)
        next_total_storage_fee = tx_storage_cost + receipt_storage_cost
        if next_total_storage_fee == total_storage_fee:
            break
        total_storage_fee = next_total_storage_fee

    return tx_storage_cost + receipt_storage_cost

