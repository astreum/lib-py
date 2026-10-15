from __future__ import annotations

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
        prev_hash = getattr(block, "previous_block_hash", None)
        if prev_hash:
            from astreum.consensus.block.encoding.decode import get_block_from_storage
            try:
                block.previous_block = get_block_from_storage(astreum_node=node, block_hash=prev_hash)
            except Exception:
                pass

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
