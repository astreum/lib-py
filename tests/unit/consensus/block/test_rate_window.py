import sys
import unittest
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.rate import (  # noqa: E402
    calculate_discount_rate,
    calculate_quote_rate,
    calculate_storage_fee,
)
from astreum.consensus.models.statistics import (  # noqa: E402
    BlockStatistics,
    all_time_rate_fraction,
)


def _previous_block(statistics, height):
    """Build a previous block that exposes the statistics value."""
    return SimpleNamespace(height=height, statistics=statistics)


def _quote_block(statistics, height):
    return SimpleNamespace(
        height=height,
        previous_block=_previous_block(statistics, height - 1),
    )


class TestUpdateStatistics(unittest.TestCase):
    def test_returns_a_statistics_value(self) -> None:
        stats = BlockStatistics().updated(total_balance=7, fee_income=3, claims=1)
        self.assertIsInstance(stats, BlockStatistics)

    def test_starts_at_zero_when_no_previous_statistics(self) -> None:
        stats = BlockStatistics().updated(total_balance=7, fee_income=3, claims=1)
        self.assertEqual(stats, BlockStatistics(3, 7, 7, 1))

    def test_accumulates_six_fields(self) -> None:
        first = BlockStatistics().updated(total_balance=7, fee_income=3, claims=1)
        second = first.updated(total_balance=4, fee_income=10, claims=2)
        self.assertEqual(second, BlockStatistics(13, 4, 11, 3))

    def test_zero_deltas_leave_statistics_unchanged_except_total_balance(self) -> None:
        first = BlockStatistics().updated(total_balance=7, fee_income=3, claims=1)
        second = first.updated(total_balance=0, fee_income=0, claims=0)
        # total_fees, total_claims, tx_fees and tx_bytes are unchanged,
        # total_balance is the new level, cumulative_total_balance grows.
        self.assertEqual(second, BlockStatistics(3, 0, 7, 1))


class TestTotalBalanceLevelSemantics(unittest.TestCase):
    def test_total_balance_is_a_level_not_a_delta(self) -> None:
        """The cumulative field grows by the level total_balance each block."""
        previous = BlockStatistics(100, 50, 50, 10)
        stats = previous.updated(total_balance=75, fee_income=0, claims=0)
        self.assertEqual(stats, BlockStatistics(100, 75, 125, 10))

    def test_consecutive_empty_blocks_grow_by_full_total_balance(self) -> None:
        """Two empty blocks with unchanged total balance B add B to D each time."""
        block1 = BlockStatistics().updated(total_balance=100, fee_income=0, claims=0)
        self.assertEqual(block1.cumulative_total_balance, 100)
        block2 = block1.updated(total_balance=100, fee_income=0, claims=0)
        self.assertEqual(block2.cumulative_total_balance, 200)


class TestRewardSupplyAccounting(unittest.TestCase):
    def test_reward_increases_cumulative_total_balance(self) -> None:
        """A reward-only block adds the full rewarded balance to D(h)."""
        previous = BlockStatistics(0, 0, 100, 0)
        stats = previous.updated(total_balance=60, fee_income=0, claims=0)
        self.assertEqual(stats.cumulative_total_balance, 160)


class TestAllTimeRateFraction(unittest.TestCase):
    def test_reads_entry_zero_from_previous_block(self) -> None:
        previous_block = SimpleNamespace(
            height=5,
            statistics=BlockStatistics(100, 50, 200, 10),
        )
        block = SimpleNamespace(previous_block=previous_block)
        fraction = all_time_rate_fraction(block)
        self.assertEqual(fraction, (100, 200))

    def test_returns_none_without_previous_block(self) -> None:
        block = SimpleNamespace(previous_block=None)
        self.assertIsNone(all_time_rate_fraction(block))

    def test_returns_none_when_statistics_empty(self) -> None:
        previous_block = SimpleNamespace(height=5, statistics=None)
        block = SimpleNamespace(previous_block=previous_block)
        self.assertIsNone(all_time_rate_fraction(block))


class TestCalculateDiscountRate(unittest.TestCase):
    def test_returns_the_settlement_ratio(self) -> None:
        block = _quote_block(BlockStatistics(3, 0, 6, 0), height=1)
        self.assertEqual(calculate_discount_rate(block), 0.5)


class TestCalculateQuoteRate(unittest.TestCase):
    """The quote is r_bar * (1 - (1 + r_bar)^-h) from the previous tuple."""

    def test_genesis_quote_is_zero(self) -> None:
        genesis = SimpleNamespace(height=0, previous_block=None)
        self.assertEqual(calculate_quote_rate(genesis), Fraction(0))

    def test_quote_is_zero_when_settlement_ratio_is_zero(self) -> None:
        block = _quote_block(BlockStatistics(0, 0, 5, 0), height=3)
        self.assertEqual(calculate_quote_rate(block), Fraction(0))

    def test_quote_matches_the_closed_form(self) -> None:
        statistics = BlockStatistics(1, 0, 2, 0)  # r_bar = 1 / 2
        r_bar = 0.5
        for height in (1, 2, 5, 12, 40):
            expected = r_bar * (1 - (1 + r_bar) ** (-height))
            actual = float(calculate_quote_rate(_quote_block(statistics, height)))
            self.assertAlmostEqual(actual, expected, places=15, msg=f"height={height}")

    def test_quote_passes_099_of_settlement_ratio_within_40_blocks(self) -> None:
        statistics = BlockStatistics(1, 0, 2, 0)  # model intensity 0.5
        r_bar = 0.5
        reached_at = None
        for height in range(1, 41):
            quote = float(calculate_quote_rate(_quote_block(statistics, height)))
            if quote >= 0.99 * r_bar:
                reached_at = height
                break
        self.assertEqual(reached_at, 12)


class TestStorageFeeAtZeroFeeIncome(unittest.TestCase):
    def test_storage_fee_is_flat_when_fee_income_is_zero(self) -> None:
        block = _quote_block(BlockStatistics(0, 0, 5, 0), height=3)
        self.assertEqual(calculate_storage_fee(block, 100), 200)


if __name__ == "__main__":
    unittest.main()
