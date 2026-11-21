from __future__ import annotations

from fractions import Fraction
from types import SimpleNamespace

from astreum.consensus.models.statistics import (
    BlockStatistics,
    all_time_rate_fraction,
)
from astreum.consensus.constants import PROVIDERS_PER_RECORD

# Fixed-point scale for the bootstrap power ``(1 + r_bar)^(-h)``. The
# quote needs one power at read time; the scale and the power method are
# precision choices, not prices. Exponentiation by squaring truncates
# every product back to the scale, so the stored power is a lower bound
# on the exact power. That lifts the quote above the closed form by a
# negligible relative amount: the quote never falls below the closed
# form and never exceeds ``r_bar``. The difference stays within a
# relative 1e-18 until the power underflows, where the quote saturates
# at ``r_bar``.
QUOTE_POWER_SCALE = 10**18


def calculate_discount_rate(block: object, node=None, range_blocks: int = 0) -> float:
    """Calculate the all-time settlement ratio of *block*.

    The ratio is ``r_bar = total_fees / cumulative_total_balance`` from
    the previous block's statistics.

    Args:
        block: The block to calculate from.
        node: Optional node for storage access.
        range_blocks: Ignored; the all-time ratio is the only rate.

    Returns:
        The settlement ratio as a float.

    Raises:
        ValueError: If rate fraction unavailable.
    """
    if node is not None and block.previous_block is None:
        from astreum.consensus.block.previous import get_previous_block
        block.previous_block = get_previous_block(node, block)

    rate_fraction = all_time_rate_fraction(block)
    if rate_fraction is None:
        raise ValueError(
            f"rate fraction not available for range_blocks={range_blocks}"
        )
    numerator, denominator = rate_fraction
    return numerator / denominator


def calculate_quote_rate(block: object) -> Fraction:
    """Return the storage quote for *block*'s height.

    The quote is ``r_bar * (1 - (1 + r_bar)^(-h))`` with ``r_bar`` the
    all-time settlement ratio from the previous block's tuple and
    ``h = block.height``. Nothing is stored: the quote is derived at
    read time from the tuple and the height.

    The bootstrap power is computed in fixed point through
    exponentiation by squaring. The scale is lifted so that
    ``scale * r_bar`` is about ``QUOTE_POWER_SCALE``, which stops
    truncation of the base from swamping the exponent ``h * r_bar`` when
    the ratio is small. Every product is truncated back to the scale, so
    the stored power is a lower bound on the exact power. That lifts the
    quote above the closed form by a negligible relative amount: the
    quote never falls below the closed form and never exceeds ``r_bar``.

    Args:
        block: The block whose height and previous tuple are read.

    Returns:
        The quote as a Fraction. It is zero at genesis and whenever the
        previous tuple is unavailable or its fee total is zero.
    """
    rate_fraction = all_time_rate_fraction(block)
    if rate_fraction is None:
        return Fraction(0)

    total_fees, cumulative_total_balance = rate_fraction
    height = block.height
    if height <= 0 or total_fees <= 0:
        return Fraction(0)

    scale = QUOTE_POWER_SCALE
    if total_fees < cumulative_total_balance:
        # Lift the scale so that ``scale * r_bar`` is about the base scale.
        scale = (scale * cumulative_total_balance) // total_fees

    # (1 + r_bar)^(-h) as an integer scaled by *scale*.
    power = scale
    base = (cumulative_total_balance * scale) // (
        cumulative_total_balance + total_fees
    )
    remaining = height
    while remaining:
        if remaining & 1:
            power = (power * base) // scale
        base = (base * base) // scale
        remaining >>= 1

    return Fraction(
        total_fees * (scale - power),
        cumulative_total_balance * scale,
    )


def calculate_fee_rate(block: object) -> Fraction:
    """Return the measured byte price of *block*.

    The price is ``fee_rate = total_tx_fees / total_tx_bytes`` from
    the previous block's statistics. The pair counts
    inclusion fees only: the base fee plus the execution fee of every
    receipt. Storage fees are not demand and are excluded.

    Args:
        block: The block with a ``previous_block`` tuple.

    Returns:
        The measured price as a Fraction.

    Raises:
        ValueError: If the previous block or its statistics are
            unavailable, or if either pair total is zero. An empty pair
            has no price.
    """
    previous_block = block.previous_block
    if previous_block is None:
        raise ValueError(
            "previous block is required for the measured byte price"
        )

    stats = previous_block.statistics
    if not stats:
        raise ValueError(
            "previous block statistics are required for the measured byte price"
        )

    total_tx_fees, total_tx_bytes = stats.total_tx_fees, stats.total_tx_bytes
    if total_tx_fees <= 0:
        raise ValueError(
            "previous block total_tx_fees must be greater than zero"
        )
    if total_tx_bytes <= 0:
        raise ValueError(
            "previous block total_tx_bytes must be greater than zero"
        )
    return Fraction(total_tx_fees, total_tx_bytes)


def calculate_storage_fee(block: object, total_bytes: int) -> int:
    """Calculate the storage fee at the measured byte price.

    The fee is ``2 * ceil(total_bytes * fee_rate / r_q)`` where
    ``fee_rate`` is the demand pair from the previous block and ``r_q``
    is the quote for the block's height. The factor of two covers
    ``PROVIDERS_PER_RECORD`` copies of the data.

    Args:
        block: The block to calculate from.
        total_bytes: Total bytes to store.

    Returns:
        The storage fee in units. The fee covers ``PROVIDERS_PER_RECORD``
        copies of the data.

    Raises:
        ValueError: If the block has no previous block, the previous block
            quote is zero, or the demand pair has no price. The fee is never
            invented.
    """
    if block.previous_block is None:
        raise ValueError("previous block is required for the storage fee")

    quote = calculate_quote_rate(block)
    if quote <= 0:
        raise ValueError(
            "previous block cumulative total fees must be greater than zero"
        )
    fee_rate = calculate_fee_rate(block)
    return storage_fee_at_rate(fee_rate, quote, total_bytes)


def estimate_storage_rate(head: object, window: int) -> Fraction:
    """Return the current all-time storage rate.

    Windowed estimation has been removed with the single-entry statistics
    change; the all-time rate is the only rate available.

    Args:
        head: The head block to estimate from.
        window: Inclusion window in blocks (ignored).

    Returns:
        The all-time storage rate as ``cumulative_total_balance / total_fees``.

    Raises:
        ValueError: If the head total fees are not greater than zero.
    """
    stats = head.statistics
    if not isinstance(stats, BlockStatistics):
        raise ValueError("head statistics are required for the storage rate")
    cumulative_fee = int(stats.total_fees)
    cumulative_total_balance = int(stats.cumulative_total_balance)
    if cumulative_fee <= 0:
        raise ValueError("head cumulative total fees must be greater than zero")

    return Fraction(cumulative_total_balance, cumulative_fee)


def storage_fee_at_rate(
    fee_rate: Fraction, quote: Fraction, walk_size: int
) -> int:
    """Calculate the storage fee for *walk_size* bytes.

    The fee is ``2 * ceil(walk_size * fee_rate / quote)``. The division
    is exact rational arithmetic, so a byte count on a ceiling boundary
    is not rounded up by float error.

    Args:
        fee_rate: The measured byte price, fee per byte.
        quote: The storage quote ``r_q``.
        walk_size: Total bytes to store.

    Returns:
        The storage fee in units. The fee covers ``PROVIDERS_PER_RECORD``
        copies of the data.

    Raises:
        ValueError: If the fee rate or the quote is not positive.
    """
    if fee_rate <= 0:
        raise ValueError("fee rate must be greater than zero")
    if quote <= 0:
        raise ValueError("quote must be greater than zero")

    numerator = walk_size * fee_rate.numerator * quote.denominator
    denominator = fee_rate.denominator * quote.numerator
    return ((numerator + denominator - 1) // denominator) * PROVIDERS_PER_RECORD


def next_storage_price(head: object) -> tuple[Fraction, Fraction]:
    """Return the storage price that applies to the block after *head*.

    The storage fee of the next block reads *head* as its previous
    block and uses ``head.height + 1`` for the quote.

    Args:
        head: The latest block.

    Returns:
        ``(fee_rate, quote)`` for the next block.

    Raises:
        ValueError: If the head has no measured demand pair.
    """
    next_block = SimpleNamespace(
        height=head.height + 1,
        previous_block=head,
    )
    return calculate_fee_rate(next_block), calculate_quote_rate(next_block)
