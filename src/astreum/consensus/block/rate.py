from __future__ import annotations

from astreum.consensus.block.rate_window import windowed_rate_fraction


def calculate_discount_rate(block: object, node=None, range_blocks: int = 0) -> float:
    """Calculate the discount rate over a window of *range_blocks* (default 0 = all-time)."""

    Args:
        block (object): Description.
        node: Description.
        range_blocks (int): Description.

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
    """Calculate storage fee from the previous block."""

    Args:
        block (object): Description.
        total_bytes (int): Description.

    previous_block = block.previous_block
    if previous_block is None:
        # TODO: return None instead of 0, propagate None through
        # add_pending_storage_contract caller chain
        return 0

    numerator = total_bytes * int(previous_block.cumulative_stake)
    denominator = int(previous_block.cumulative_total_fee)
    if denominator <= 0:
        raise ValueError("previous block cumulative total fees must be greater than zero")

    return (numerator + denominator - 1) // denominator
