from __future__ import annotations

from typing import Iterable


def validator_reward(receipts: Iterable) -> int:
    """Return what the validator of a block is paid.

    The validator gets the transaction fees and the data fees of the block,
    with a floor of 1 for a block that pays no fees. The storage fees go to
    the storage account, not to the validator.

    Args:
        receipts: The receipts of the block.

    Returns:
        The reward in aster.
    """
    paid = sum(r.transaction_fee + r.data_fee for r in receipts)
    return max(paid, 1)


def fee_income(receipts: Iterable) -> int:
    """Return the fee income a block adds to the chain statistics.

    It is the validator reward plus the storage fees, so a block with no
    transactions adds its base reward and a paid storage fee raises the rate.

    Args:
        receipts: The receipts of the block.

    Returns:
        The fee income in aster.
    """
    receipts = list(receipts)
    return validator_reward(receipts) + sum(r.storage_fee for r in receipts)
