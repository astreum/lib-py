from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class BlockStatistics:
    """The all-time figures a block commits to.

    The field order is the wire order. ``cumulative_total_balance`` is the
    only value that adds to itself every block; every other all-time sum uses
    the ``total_`` prefix and grows only when an event happens.

    Attributes:
        total_fees: Fee income, ever.
        total_balance: The supply level ``S(h)`` at this block.
        cumulative_total_balance: The sum of the supply levels, ever.
        total_claims: Storage claims, ever.
        total_tx_fees: Inclusion fees, ever.
        total_tx_bytes: Included transaction bytes, ever.
        total_loan_count: Loans created, ever.
        total_loaned: Principal minted, ever.
        total_written_off: Money forgiven, ever.
    """

    total_fees: int = 0
    total_balance: int = 0
    cumulative_total_balance: int = 0
    total_claims: int = 0
    total_tx_fees: int = 0
    total_tx_bytes: int = 0
    total_loan_count: int = 0
    total_loaned: int = 0
    total_written_off: int = 0

    def __post_init__(self) -> None:
        """Reject a value that is not a non-negative integer.

        Raises:
            ValueError: If a field fails the check.
        """
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(
                    f"statistics {name} must be a non-negative integer, "
                    f"got {value!r}"
                )

    def with_loan(self, principal: int) -> "BlockStatistics":
        """Return the value after one loan is created.

        Args:
            principal: The principal the new loan minted.

        Returns:
            The value with one more loan and its principal.
        """
        return replace(
            self,
            total_loan_count=self.total_loan_count + 1,
            total_loaned=self.total_loaned + principal,
        )

    def with_write_off(self, amount: int) -> "BlockStatistics":
        """Return the value after money is forgiven.

        Args:
            amount: The amount written off.

        Returns:
            The value with the write-off counted.
        """
        return replace(
            self,
            total_written_off=self.total_written_off + amount,
        )

    def updated(
        self,
        *,
        total_balance: int,
        fee_income: int,
        claims: int,
        tx_fees: int,
        tx_bytes: int,
    ) -> "BlockStatistics":
        """Return the value after one block adds its events.

        The level replaces ``total_balance``; the other deltas add to their
        counters.

        Args:
            total_balance: The supply level ``S(h)`` of this block.
            fee_income: Fee income this block added.
            claims: Storage claims this block added.
            tx_fees: Inclusion fees this block paid.
            tx_bytes: Included transaction bytes this block carried.

        Returns:
            The value for this block.
        """
        return replace(
            self,
            total_fees=self.total_fees + fee_income,
            total_balance=total_balance,
            cumulative_total_balance=self.cumulative_total_balance + total_balance,
            total_claims=self.total_claims + claims,
            total_tx_fees=self.total_tx_fees + tx_fees,
            total_tx_bytes=self.total_tx_bytes + tx_bytes,
        )


def all_time_rate_fraction(block: object) -> tuple[int, int] | None:
    """Return the all-time rate fraction of the previous block.

    Args:
        block: The current block, which holds ``previous_block``.

    Returns:
        The ``(total_fees, cumulative_total_balance)`` fraction, or None
        when the previous block or its statistics are missing.
    """
    previous_block = getattr(block, "previous_block", None)
    if previous_block is None:
        return None
    stats = getattr(previous_block, "statistics", None)
    if stats is None or stats.cumulative_total_balance <= 0:
        return None
    return stats.total_fees, stats.cumulative_total_balance
