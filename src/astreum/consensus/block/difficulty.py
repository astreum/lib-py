from typing import Optional


def calculate_block_difficulty(
    previous_timestamp: Optional[int],
    current_timestamp: Optional[int],
    previous_difficulty: Optional[int],
    target_spacing: int = 2,
) -> int:
    """Calculate the difficulty for a new block.

    Based on the previous block's timestamp, current timestamp, and previous
    difficulty. If spacing is very short (<= 1s), difficulty increases by 1.
    If spacing exceeds the target, difficulty decreases by 1 (floor 1).

    Args:
        previous_timestamp: Timestamp of the previous block, or None for genesis.
        current_timestamp: Timestamp of the new block, or None.
        previous_difficulty: Difficulty of the previous block, or None for genesis.
        target_spacing: Target block spacing in seconds (default 2).

    Returns:
        The new difficulty value (minimum 1).
    """
    base_difficulty = max(1, previous_difficulty or 1)
    if previous_timestamp is None or current_timestamp is None:
        return base_difficulty

    spacing = max(0, current_timestamp - previous_timestamp)
    if spacing <= 1:
        return base_difficulty + 1
    if spacing > target_spacing:
        return max(1, base_difficulty - 1)
    return base_difficulty
