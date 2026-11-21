"""Genesis seeds the supply but no fee the chain did not collect."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.validation.genesis import create_genesis_block  # noqa: E402
from astreum.consensus.models.statistics import BlockStatistics  # noqa: E402
from astreum.node import Node  # noqa: E402


class TestGenesisStatistics(unittest.TestCase):
    def test_genesis_has_no_fee_seed(self):
        node = Node()
        block = create_genesis_block(node, b"\x07" * 32)
        self.assertEqual(block.statistics, BlockStatistics(0, 1, 1, 0))
        self.assertEqual(block.statistics.total_fees, 0)


if __name__ == "__main__":
    unittest.main()
