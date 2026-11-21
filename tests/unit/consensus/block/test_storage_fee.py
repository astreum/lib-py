"""The storage fee is a flat one astre per byte per provider copy."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.rate import calculate_storage_fee, storage_fee  # noqa: E402
from astreum.consensus.constants import PROVIDERS_PER_RECORD  # noqa: E402


class TestStorageFee(unittest.TestCase):
    def test_the_fee_is_one_astre_per_byte_per_copy(self):
        self.assertEqual(PROVIDERS_PER_RECORD, 2)
        block = SimpleNamespace(previous_block=SimpleNamespace())
        for total_bytes in (0, 1, 101, 5000):
            self.assertEqual(
                calculate_storage_fee(block, total_bytes),
                PROVIDERS_PER_RECORD * total_bytes,
                total_bytes,
            )

    def test_the_fee_does_not_read_the_statistics(self):
        block = SimpleNamespace(
            previous_block=SimpleNamespace(statistics=None)
        )
        self.assertEqual(calculate_storage_fee(block, 101), 202)

    def test_the_fee_is_zero_without_a_previous_block(self):
        block = SimpleNamespace(previous_block=None)
        self.assertEqual(calculate_storage_fee(block, 1000), 0)

    def test_storage_fee_uses_the_same_flat_rate(self):
        for total_bytes in (0, 1, 101, 5000):
            self.assertEqual(
                storage_fee(total_bytes),
                PROVIDERS_PER_RECORD * total_bytes,
            )


if __name__ == "__main__":
    unittest.main()
