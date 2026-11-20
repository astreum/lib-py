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
from astreum.consensus.block.rate_window import (  # noqa: E402
    all_time_rate_fraction,
    update_statistics,
)


def _previous_block(statistics, height):
    """Build a previous block that exposes both the tuple and the accessors."""
    total_fees, _, cumulative_total_balance = statistics[0][:3]
    return SimpleNamespace(
        height=height,
        statistics=statistics,
        cumulative_total_fee=total_fees,
        cumulative_total_balance=cumulative_total_balance,
    )


def _quote_block(statistics, height):
    return SimpleNamespace(
        height=height,
        previous_block=_previous_block(statistics, height - 1),
    )


class TestUpdateStatistics(unittest.TestCase):
    def test_returns_single_entry(self) -> None:
        stats = update_statistics(None, 7, 3, 1, 5, 9)
        self.assertEqual(len(stats), 1)

    def test_starts_at_zero_when_no_previous_statistics(self) -> None:
        stats = update_statistics(None, 7, 3, 1, 5, 9)
        self.assertEqual(stats[0], (3, 7, 7, 1, 5, 9))

    def test_accumulates_six_fields(self) -> None:
        first = update_statistics(None, 7, 3, 1, 5, 9)
        second = update_statistics(first, 4, 10, 2, 3, 6)
        self.assertEqual(second[0], (13, 4, 11, 3, 8, 15))

    def test_zero_deltas_leave_statistics_unchanged_except_total_balance(self) -> None:
        first = update_statistics(None, 7, 3, 1, 5, 9)
        second = update_statistics(first, 0, 0, 0, 0, 0)
        # total_fees, total_claims, tx_fees and tx_bytes are unchanged,
        # total_balance is the new level, cumulative_total_balance grows.
        self.assertEqual(second[0], (3, 0, 7, 1, 5, 9))


class TestTotalBalanceLevelSemantics(unittest.TestCase):
    def test_total_balance_is_a_level_not_a_delta(self) -> None:
        """The cumulative field grows by the level total_balance each block."""
        prev_statistics = [(100, 50, 50, 10, 20, 30)]
        current_total_balance = 75
        stats = update_statistics(prev_statistics, current_total_balance, 0, 0, 0, 0)
        self.assertEqual(stats[0], (100, 75, 125, 10, 20, 30))

    def test_consecutive_empty_blocks_grow_by_full_total_balance(self) -> None:
        """Two empty blocks with unchanged total balance B add B to D each time."""
        initial = [(0, 0, 0, 0, 0, 0)]
        block1 = update_statistics(initial, 100, 0, 0, 0, 0)
        self.assertEqual(block1[0][2], 100)
        block2 = update_statistics(block1, 100, 0, 0, 0, 0)
        self.assertEqual(block2[0][2], 200)


class TestRewardSupplyAccounting(unittest.TestCase):
    def test_reward_increases_cumulative_total_balance(self) -> None:
        """A reward-only block adds the full rewarded balance to D(h)."""
        prev_statistics = [(0, 0, 100, 0, 0, 0)]
        current_total_balance = 60
        stats = update_statistics(prev_statistics, current_total_balance, 0, 0, 0, 0)
        self.assertEqual(stats[0][2], 160)


class TestDemandPairUpdate(unittest.TestCase):
    def test_empty_block_leaves_the_pair_unchanged(self) -> None:
        previous = [(3, 7, 11, 1, 5, 9)]
        stats = update_statistics(previous, 7, 0, 0, 0, 0)
        self.assertEqual(stats[0][4:], (5, 9))

    def test_deltas_accumulate_the_pair(self) -> None:
        previous = [(3, 7, 11, 1, 5, 9)]
        stats = update_statistics(previous, 7, 0, 0, 3, 6)
        self.assertEqual(stats[0][4:], (8, 15))


class TestAllTimeRateFraction(unittest.TestCase):
    def test_reads_entry_zero_from_previous_block(self) -> None:
        previous_block = SimpleNamespace(
            height=5,
            statistics=[(100, 50, 200, 10, 20, 30)],
        )
        block = SimpleNamespace(previous_block=previous_block)
        fraction = all_time_rate_fraction(block)
        self.assertEqual(fraction, (100, 200))

    def test_returns_none_without_previous_block(self) -> None:
        block = SimpleNamespace(previous_block=None)
        self.assertIsNone(all_time_rate_fraction(block))

    def test_returns_none_when_statistics_empty(self) -> None:
        previous_block = SimpleNamespace(height=5, statistics=[])
        block = SimpleNamespace(previous_block=previous_block)
        self.assertIsNone(all_time_rate_fraction(block))


class TestCalculateDiscountRate(unittest.TestCase):
    def test_returns_the_settlement_ratio(self) -> None:
        block = _quote_block([(3, 0, 6, 0, 0, 0)], height=1)
        self.assertEqual(calculate_discount_rate(block), 0.5)


class TestCalculateQuoteRate(unittest.TestCase):
    """The quote is r_bar * (1 - (1 + r_bar)^-h) from the previous tuple."""

    def test_genesis_quote_is_zero(self) -> None:
        genesis = SimpleNamespace(height=0, previous_block=None)
        self.assertEqual(calculate_quote_rate(genesis), Fraction(0))

    def test_quote_is_zero_when_settlement_ratio_is_zero(self) -> None:
        block = _quote_block([(0, 0, 5, 0, 0, 0)], height=3)
        self.assertEqual(calculate_quote_rate(block), Fraction(0))

    def test_quote_matches_the_closed_form(self) -> None:
        statistics = [(1, 0, 2, 0, 0, 0)]  # r_bar = 1 / 2
        r_bar = 0.5
        for height in (1, 2, 5, 12, 40):
            expected = r_bar * (1 - (1 + r_bar) ** (-height))
            actual = float(calculate_quote_rate(_quote_block(statistics, height)))
            self.assertAlmostEqual(actual, expected, places=15, msg=f"height={height}")

    def test_quote_passes_099_of_settlement_ratio_within_40_blocks(self) -> None:
        statistics = [(1, 0, 2, 0, 0, 0)]  # model intensity 0.5
        r_bar = 0.5
        reached_at = None
        for height in range(1, 41):
            quote = float(calculate_quote_rate(_quote_block(statistics, height)))
            if quote >= 0.99 * r_bar:
                reached_at = height
                break
        self.assertEqual(reached_at, 12)


class TestStorageFeeAtZeroFeeIncome(unittest.TestCase):
    def test_storage_fee_raises_when_fee_income_is_zero(self) -> None:
        block = _quote_block([(0, 0, 5, 0, 0, 0)], height=3)
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            calculate_storage_fee(block, 100)


if __name__ == "__main__":
    unittest.main()
