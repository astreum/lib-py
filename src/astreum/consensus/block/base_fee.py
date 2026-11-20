"""Estimate the transaction base fee to bid."""

from __future__ import annotations

import math
from typing import Any

from astreum.consensus.block.previous import get_previous_block
from astreum.crypto.bloom_search.block_search import find_block_by_height
from astreum.crypto.bloom_search.search import ERA_SIZE


def mean_base_fee_per_transaction(block: Any) -> int:
    """Mean base fee paid per transaction in one block.

    Args:
        block: The block to read.

    Returns:
        The mean base fee in aster, or 0 when the block has no transactions.
    """
    count = block.transaction_count
    if not count:
        return 0
    return block.total_base_fee // count


def estimate_transaction_base_fee(
    node: Any, head: Any, *, window: int | None = None, samples: int | None = None
) -> int:
    """Estimate the base fee to bid for early inclusion.

    The estimate is the weighted mean of the base fees accepted in a window of
    previous blocks. It is advisory: consensus checks only ``base_fee >= 1``.

    Args:
        node: The node, used to load the sampled blocks.
        head: The block to estimate from. Sampling starts at its parent.
        window: Blocks to look back. None uses
            ``node.config.storage_claim_fee_window``.
        samples: Blocks to read. None uses
            ``node.config.storage_claim_fee_samples``.

    Returns:
        The estimated base fee in aster, at least 1.
    """
    window = window or node.config.storage_claim_fee_window
    samples = samples or node.config.storage_claim_fee_samples

    stride = max(1, window // samples)
    while math.gcd(stride, ERA_SIZE) != 1:
        stride -= 1

    total_fee = 0
    total_count = 0
    target = (head.height or 0) - 1
    block = get_previous_block(node, head)
    for i in range(samples):
        if block is None or target < 0:
            break
        total_fee += block.total_base_fee
        total_count += block.transaction_count
        target -= stride
        if i + 1 < samples and target >= 0:
            block = find_block_by_height(
                node, starting_block=block, target_height=target
            )

    if total_count <= 0:
        return 1
    return total_fee // total_count
