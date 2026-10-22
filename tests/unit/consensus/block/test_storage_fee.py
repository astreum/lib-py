"""The storage fee covers PROVIDERS_PER_RECORD copies of the data."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.constants import PROVIDERS_PER_RECORD


def _block(stake, total_fee):
    previous = SimpleNamespace(cumulative_stake=stake, cumulative_total_fee=total_fee)
    return SimpleNamespace(previous_block=previous)


class TestStorageFee(unittest.TestCase):
    def test_fee_is_double_the_single_provider_fee(self):
        self.assertEqual(PROVIDERS_PER_RECORD, 2)
        for total_bytes, stake, total_fee in ((100, 7, 3), (1, 1, 1), (5000, 13, 29), (0, 4, 9)):
            single = -(-total_bytes * stake // total_fee)  # ceil
            self.assertEqual(
                calculate_storage_fee(_block(stake, total_fee), total_bytes),
                2 * single,
                (total_bytes, stake, total_fee),
            )

    def test_fee_zero_without_previous_block(self):
        self.assertEqual(calculate_storage_fee(SimpleNamespace(previous_block=None), 1000), 0)

    def test_fee_raises_on_zero_total_fee(self):
        with self.assertRaises(ValueError):
            calculate_storage_fee(_block(5, 0), 100)


if __name__ == "__main__":
    unittest.main()
