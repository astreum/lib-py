"""Fee settlement for a top-level transaction.

The rule:

1. A transaction owes three fees: the transaction fee (base fee plus
   evaluation fee), the data fee and the storage fee.
2. The sender pays all owed fees and the transfer amount, or the
   transaction fails.
3. A failed transaction still pays. It pays ``min(balance, owed)``. The
   sender never gets a free failure because it could not afford the full
   fee. If the balance is lower than a single fee, the sender pays the
   whole balance.
4. The fees are paid in this order: transaction fee, data fee, storage
   fee. A fee that the balance cannot reach in full is paid in part, and
   the fees after it are not paid.
5. A failed transaction never moves the transfer amount.
6. Only the part of the storage fee that was paid goes to the storage
   account.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeSettlement:
    """The amounts taken from a sender.

    Attributes:
        transaction_fee: The transaction fee paid.
        data_fee: The data fee paid.
        storage_fee: The storage fee paid. This amount goes to the storage account.
        transfer_amount: The transfer amount moved. It is 0 for a failed transaction.
        affordable: True if the sender paid every fee in full and the transfer.
    """

    transaction_fee: int
    data_fee: int
    storage_fee: int
    transfer_amount: int
    affordable: bool

    @property
    def total(self) -> int:
        """Get the amount taken from the sender.

        Args:
            None.

        Returns:
            The paid fees plus the moved transfer amount.
        """
        return (
            self.transaction_fee
            + self.data_fee
            + self.storage_fee
            + self.transfer_amount
        )


def settle_fees(
    balance: int,
    transaction_fee: int,
    data_fee: int,
    storage_fee: int,
    transfer_amount: int,
) -> FeeSettlement:
    """Decide how much of each fee a sender pays.

    See the module docstring for the rule.

    Args:
        balance: The sender balance.
        transaction_fee: The transaction fee owed.
        data_fee: The data fee owed.
        storage_fee: The storage fee owed.
        transfer_amount: The transfer amount, 0 if none.

    Returns:
        The amounts to take from the sender.
    """
    owed = transaction_fee + data_fee + storage_fee
    if balance >= owed + transfer_amount:
        return FeeSettlement(
            transaction_fee, data_fee, storage_fee, transfer_amount, True
        )
    remaining = max(0, balance)
    paid = []
    for due in (transaction_fee, data_fee, storage_fee):
        part = min(remaining, due)
        paid.append(part)
        remaining -= part
    return FeeSettlement(paid[0], paid[1], paid[2], 0, False)
