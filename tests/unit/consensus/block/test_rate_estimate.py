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
    estimate_storage_rate,
    storage_fee_at_rate,
)

def _head(fee=100, stake=200, height=9):
    """Build a fake head with single-entry statistics."""
    statistics = [(fee, stake, stake, 0, 0, 0)]
    return SimpleNamespace(
        height=height,
        statistics=statistics,
        cumulative_total_fee=fee,
        cumulative_total_balance=stake,
    )


class EstimateStorageRateTests(unittest.TestCase):
    def test_rate_rising(self):
        head = _head()
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_falling(self):
        head = _head()
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_window_rounded_down(self):
        head = _head()
        self.assertEqual(estimate_storage_rate(head, 6), Fraction(2))

    def test_rate_window_past_chain(self):
        head = _head()
        self.assertEqual(estimate_storage_rate(head, 65536), Fraction(2))

    def test_rate_no_window(self):
        head = _head(height=0)
        self.assertEqual(len(head.statistics), 1)
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_then_no_fee(self):
        head = _head()
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_no_fees(self):
        head = _head(fee=0)
        with self.assertRaises(ValueError):
            estimate_storage_rate(head, 4)


class StorageFeeAtRateTests(unittest.TestCase):
    def test_fee_at_rate(self):
        self.assertEqual(
            storage_fee_at_rate(Fraction(3, 2), Fraction(1, 2), 10), 60
        )
        self.assertEqual(
            storage_fee_at_rate(Fraction(1, 3), Fraction(2, 5), 10), 18
        )

    def test_fee_at_rate_rejects_a_nonpositive_fee_rate(self):
        with self.assertRaises(ValueError):
            storage_fee_at_rate(Fraction(0), Fraction(1), 10)

    def test_fee_at_rate_rejects_a_nonpositive_quote(self):
        with self.assertRaises(ValueError):
            storage_fee_at_rate(Fraction(1), Fraction(0), 10)


if __name__ == "__main__":
    unittest.main()
