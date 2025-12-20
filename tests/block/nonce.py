import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.validation.models.block import Block  # noqa: E402
from astreum.storage.models.atom import ZERO32  # noqa: E402


class TestBlockNonce(unittest.TestCase):
    def test_generate_nonce_difficulty_one(self) -> None:
        block = Block(
            chain_id=0,
            previous_block_hash=ZERO32,
            previous_block=None,
            number=0,
            timestamp=1,
            accounts_hash=ZERO32,
            transactions_total_fees=0,
            transactions_hash=None,
            receipts_hash=None,
            delay_difficulty=1,
            validator_public_key=None,
            nonce=0,
        )

        nonce = block.generate_nonce(difficulty=1)
        self.assertEqual(block.nonce, nonce)
        self.assertGreaterEqual(nonce, 0)
        self.assertIsNotNone(block.atom_hash)

        leading_zeros = Block._leading_zero_bits(block.atom_hash)
        self.assertGreaterEqual(leading_zeros, 1)
