from __future__ import annotations

from fractions import Fraction

from astreum.consensus.block.rate_window import windowed_rate_fraction
from astreum.consensus.constants import PROVIDERS_PER_RECORD


def calculate_discount_rate(block: object, node=None, range_blocks: int = 0) -> float:
    """Calculate the discount rate over a window of *range_blocks*.

    Args:
        block: The block to calculate from.
        node: Optional node for storage access.
        range_blocks: Window size in blocks (0 for all-time).

    Returns:
        The discount rate as a float.

    Raises:
        ValueError: If rate fraction unavailable.
    """
    if node is not None and block.previous_block is None:
        from astreum.consensus.block.previous import get_previous_block
        block.previous_block = get_previous_block(node, block)

    rate_fraction = windowed_rate_fraction(block, range_blocks)
    if rate_fraction is None:
        raise ValueError(
            f"rate fraction not available for range_blocks={range_blocks}"
        )
    numerator, denominator = rate_fraction
    return numerator / denominator


def calculate_storage_fee(block: object, total_bytes: int) -> int:
    """Calculate storage fee from the previous block.

    Args:
        block: The block to calculate from.
        total_bytes: Total bytes to store.

    Returns:
        The storage fee in units. The fee covers ``PROVIDERS_PER_RECORD``
        copies of the data.

    Raises:
        ValueError: If previous block fees are invalid.
    """
    previous_block = block.previous_block
    if previous_block is None:
        # TODO: return None instead of 0, propagate None through
        # add_pending_storage_contract caller chain
        return 0

    numerator = total_bytes * int(previous_block.cumulative_stake)
    denominator = int(previous_block.cumulative_total_fee)
    if denominator <= 0:
        raise ValueError("previous block cumulative total fees must be greater than zero")

    return ((numerator + denominator - 1) // denominator) * PROVIDERS_PER_RECORD


def estimate_storage_rate(head: object, window: int) -> Fraction:
    """Estimate the storage rate paid by a transaction included within *window* blocks.

    Args:
        head: The head block to estimate from.
        window: Inclusion window in blocks. It is rounded down to a power of
            two and limited to the largest window in ``head.statistics``.

    Returns:
        The current rate plus any growth of the rate over the window.

    Raises:
        ValueError: If the head cumulative total fee is not greater than zero.
    """
    cumulative_fee = int(head.cumulative_total_fee)
    cumulative_stake = int(head.cumulative_stake)
    if cumulative_fee <= 0:
        raise ValueError("head cumulative total fees must be greater than zero")

    rate_now = Fraction(cumulative_stake, cumulative_fee)
    if len(head.statistics) < 2:
        return rate_now

    k = min(1 << (window.bit_length() - 1), 1 << (len(head.statistics) - 2))
    prev_fee, prev_stake, curr_fee, curr_stake = head.statistics[k.bit_length()][:4]
    remaining = k - 1 - head.height % k
    win_fee = curr_fee + prev_fee * remaining // k
    win_stake = curr_stake + prev_stake * remaining // k

    then_fee = cumulative_fee - win_fee
    if then_fee <= 0:
        return rate_now

    rate_then = Fraction(cumulative_stake - win_stake, then_fee)
    return rate_now + max(Fraction(0), rate_now - rate_then)


def storage_fee_at_rate(rate: Fraction, walk_size: int) -> int:
    """Calculate the storage fee for *walk_size* bytes at *rate*.

    Args:
        rate: The storage rate as stake per fee.
        walk_size: Total bytes to store.

    Returns:
        The storage fee in units. The fee covers ``PROVIDERS_PER_RECORD``
        copies of the data.
    """
    numerator = walk_size * rate.numerator
    return ((numerator + rate.denominator - 1) // rate.denominator) * PROVIDERS_PER_RECORD
