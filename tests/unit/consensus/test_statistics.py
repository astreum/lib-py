import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.statistics import (  # noqa: E402
    BlockStatistics,
    all_time_rate_fraction,
)


class TestUpdated(unittest.TestCase):
    def test_first_entry_starts_at_zero(self):
        stats = BlockStatistics().updated(
            total_balance=7, fee_income=3, claims=1, tx_fees=5, tx_bytes=9
        )
        self.assertEqual(stats, BlockStatistics(3, 7, 7, 1, 5, 9))

    def test_accumulates_the_counters(self):
        first = BlockStatistics().updated(
            total_balance=7, fee_income=3, claims=1, tx_fees=5, tx_bytes=9
        )
        second = first.updated(
            total_balance=4, fee_income=10, claims=2, tx_fees=3, tx_bytes=6
        )
        self.assertEqual(second, BlockStatistics(13, 4, 11, 3, 8, 15))

    def test_the_level_is_replaced_not_added(self):
        first = BlockStatistics(100, 50, 50, 10, 20, 30)
        second = first.updated(
            total_balance=75, fee_income=0, claims=0, tx_fees=0, tx_bytes=0
        )
        self.assertEqual(second, BlockStatistics(100, 75, 125, 10, 20, 30))

    def test_the_loan_counters_survive_the_update(self):
        block = SimpleNamespace(
            statistics=BlockStatistics(
                total_loan_count=1, total_loaned=500, total_written_off=70
            ),
            total_balance=10,
        )
        stats = (block.statistics or BlockStatistics()).updated(
            total_balance=block.total_balance,
            fee_income=0,
            claims=0,
            tx_fees=0,
            tx_bytes=0,
        )
        self.assertEqual(stats.total_loan_count, 1)
        self.assertEqual(stats.total_loaned, 500)
        self.assertEqual(stats.total_written_off, 70)


class TestValidation(unittest.TestCase):
    def test_rejects_a_negative_value(self):
        with self.assertRaises(ValueError):
            BlockStatistics(total_fees=-1)

    def test_rejects_a_non_integer(self):
        with self.assertRaises(ValueError):
            BlockStatistics(total_fees=1.5)

    def test_rejects_a_bool(self):
        with self.assertRaises(ValueError):
            BlockStatistics(total_fees=True)

    def test_the_entry_has_nine_fields(self):
        self.assertEqual(len(BlockStatistics.__dataclass_fields__), 9)


class TestRateFraction(unittest.TestCase):
    def test_reads_the_previous_block(self):
        previous = SimpleNamespace(
            statistics=BlockStatistics(100, 50, 200, 10, 20, 30)
        )
        self.assertEqual(
            all_time_rate_fraction(SimpleNamespace(previous_block=previous)),
            (100, 200),
        )

    def test_returns_none_without_a_previous_block(self):
        self.assertIsNone(
            all_time_rate_fraction(SimpleNamespace(previous_block=None))
        )

    def test_returns_none_without_statistics(self):
        previous = SimpleNamespace(statistics=None)
        self.assertIsNone(
            all_time_rate_fraction(SimpleNamespace(previous_block=previous))
        )

    def test_returns_none_when_the_denominator_is_zero(self):
        previous = SimpleNamespace(
            statistics=BlockStatistics(100, 50, 0, 0, 0, 0)
        )
        self.assertIsNone(
            all_time_rate_fraction(SimpleNamespace(previous_block=previous))
        )


class TestLoanCounters(unittest.TestCase):
    def test_with_loan_counts_the_loan_and_its_principal(self):
        stats = BlockStatistics().with_loan(500)
        self.assertEqual(stats.total_loan_count, 1)
        self.assertEqual(stats.total_loaned, 500)

    def test_with_write_off_adds_the_amount(self):
        stats = BlockStatistics().with_write_off(70)
        self.assertEqual(stats.total_written_off, 70)


if __name__ == "__main__":
    unittest.main()
