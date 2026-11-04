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

WINDOW_INDEX = {1: 1, 2: 2, 4: 3, 8: 4}


def _head(fee=100, stake=200, windows=None, height=9):
    """Build a fake head with entry 0 and entries for windows 1, 2, 4, 8."""
    statistics = [(fee, stake, 0, 0, 0, 0)]
    if height > 0:
        statistics.extend([(0, 0, 0, 0, 0, 0)] * 4)
    for window, values in (windows or {}).items():
        statistics[WINDOW_INDEX[window]] = values
    return SimpleNamespace(
        height=height,
        statistics=statistics,
        cumulative_total_fee=fee,
        cumulative_stake=stake,
    )


class EstimateStorageRateTests(unittest.TestCase):
    def test_rate_rising(self):
        head = _head(windows={4: (0, 80, 0, 10, 0, 0)})
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(5, 2))

    def test_rate_falling(self):
        head = _head(windows={4: (80, 0, 10, 0, 0, 0)})
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_window_rounded_down(self):
        head = _head(windows={4: (0, 80, 0, 10, 0, 0)})
        self.assertEqual(estimate_storage_rate(head, 6), Fraction(5, 2))

    def test_rate_window_past_chain(self):
        head = _head(windows={8: (0, 40, 0, 20, 0, 0)})
        self.assertEqual(estimate_storage_rate(head, 65536), Fraction(5, 2))

    def test_rate_no_window(self):
        head = _head(height=0)
        self.assertEqual(len(head.statistics), 1)
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_then_no_fee(self):
        head = _head(windows={4: (0, 0, 100, 0, 0, 0)})
        self.assertEqual(estimate_storage_rate(head, 4), Fraction(2))

    def test_rate_no_fees(self):
        head = _head(fee=0)
        with self.assertRaises(ValueError):
            estimate_storage_rate(head, 4)


class StorageFeeAtRateTests(unittest.TestCase):
    def test_fee_at_rate(self):
        self.assertEqual(storage_fee_at_rate(Fraction(3, 2), 10), 30)
        self.assertEqual(storage_fee_at_rate(Fraction(1, 3), 10), 8)


if __name__ == "__main__":
    unittest.main()
