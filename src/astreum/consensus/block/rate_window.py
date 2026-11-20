from __future__ import annotations


def update_statistics(
    prev_statistics: list | None,
    total_balance: int,
    delta_fees: int,
    delta_claims: int,
    delta_tx_fees: int,
    delta_tx_bytes: int,
) -> list[tuple[int, int, int, int, int, int]]:
    """Return a single all-time statistics entry.

    The ``total_balance`` argument is the full current sum of all account
    balances (a level, not a delta). The cumulative total balance is derived
    from the previous cumulative total balance plus the current level.

    Args:
        prev_statistics: Previous block's statistics list, or None.
        total_balance: Full current account-balance supply S(h), a level.
        delta_fees: Fee income added this block.
        delta_claims: Number of storage claims added this block.
        delta_tx_fees: Sum of transaction fees paid this block.
        delta_tx_bytes: Sum of transaction byte sizes this block.

    Returns:
        A one-element list containing the 6-tuple
        ``(total_fees, total_balance, cumulative_total_balance,
        total_claims, total_tx_fees, total_tx_bytes)``.
    """
    if prev_statistics and prev_statistics[0]:
        (
            total_fees,
            _,
            cumulative_total_balance,
            total_claims,
            total_tx_fees,
            total_tx_bytes,
        ) = prev_statistics[0]
    else:
        total_fees = 0
        cumulative_total_balance = 0
        total_claims = 0
        total_tx_fees = 0
        total_tx_bytes = 0

    return [
        (
            total_fees + delta_fees,
            total_balance,
            cumulative_total_balance + total_balance,
            total_claims + delta_claims,
            total_tx_fees + delta_tx_fees,
            total_tx_bytes + delta_tx_bytes,
        )
    ]


def all_time_rate_fraction(block: object) -> tuple[int, int] | None:
    """Return the all-time rate fraction from the previous block.

    The fraction is ``(total_fees, cumulative_total_balance)`` from entry 0
    of the previous block's statistics.

    Args:
        block: The current block with a ``previous_block`` attribute.

    Returns:
        A ``(numerator, denominator)`` tuple, or None when the
        previous block or its statistics are unavailable.
    """
    previous_block = getattr(block, "previous_block", None)
    if previous_block is None:
        return None

    stats = getattr(previous_block, "statistics", None)
    if not stats:
        return None

    total_fees, _, cumulative_total_balance = stats[0][:3]
    if cumulative_total_balance <= 0:
        return None

    return total_fees, cumulative_total_balance
