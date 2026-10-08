from __future__ import annotations

from typing import Any, Callable

from astreum.consensus.models.receipt import STATUS_SUCCESS, Receipt
from astreum.consensus.transaction.model import Transaction
from astreum.consensus.transaction.storage.initial import storage_walk_fee
from astreum.expression import ZERO32


def receipt_at_fixed_point(
    node: Any,
    block: object,
    build: Callable[[int], Receipt],
    base_storage_fee: int,
    named: tuple[bytes, ...],
) -> Receipt | None:
    """Receipt whose ``storage_fee`` includes the fee on its own size.

    ``build(storage_fee)`` makes the receipt. Its size grows with the value of
    ``storage_fee`` and the fee grows with its size, so start from the fees so
    far and repeat until the value stops changing. The sequence never
    decreases; after a bounded number of rounds the larger value is taken.
    ``named`` are the hashes the receipt only refers to (the transaction and
    its logs): they are charged on their own. Returns None if the receipt
    cannot be sized.
    """
    storage_fee = base_storage_fee
    receipt = build(storage_fee)
    for _ in range(8):
        own_fee = storage_walk_fee(node, block, receipt.expr(), exclude=named)
        if own_fee is None:
            return None
        next_fee = base_storage_fee + own_fee
        if next_fee == storage_fee:
            return receipt
        storage_fee = max(storage_fee, next_fee)
        receipt = build(storage_fee)
    return receipt


def calculate_transaction_costs(
    *, block: object, transaction: Transaction, node: Any = None
) -> int:
    """Mandatory storage cost of a transaction with no logs: its own fee plus
    its receipt's, both on the size the block record counts for them.

    *transaction* must be held whole (as a client builds it), so *node* is only
    needed if it refers to cells it does not hold.
    """
    tx_fee = storage_walk_fee(node, block, transaction.expr())
    if tx_fee is None:
        raise ValueError("transaction cannot be sized")

    tx_hash = transaction.hash or transaction.expr().hash()

    def build(storage_fee: int) -> Receipt:
        """Build a receipt with the given storage fee.

        Args:
            storage_fee: The storage fee.

        Returns:
            The constructed Receipt.
        """
        return Receipt(
            transaction_hash=tx_hash,
            transaction_fee=1,
            storage_fee=storage_fee,
            data_fee=0,
            execution_fee=0,
            status=STATUS_SUCCESS,
            logs_hash=ZERO32,
        )

    receipt = receipt_at_fixed_point(
        node, block, build, tx_fee, (tx_hash, ZERO32)
    )
    if receipt is None:
        raise ValueError("receipt cannot be sized")
    return receipt.storage_fee
