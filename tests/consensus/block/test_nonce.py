import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.nonce import generate_block_nonce  # noqa: E402
from astreum.consensus.block.utils.bits import count_leading_zero_bits  # noqa: E402
from astreum.expression import ZERO32  # noqa: E402


class TestBlockNonce(unittest.TestCase):
    def test_generate_nonce_difficulty_one(self) -> None:
        block = create_block(
            chain_id=0,
            previous_block_hash=ZERO32,
            previous_block=None,
            height=0,
            timestamp=1,
            accounts_hash=ZERO32,
            total_transaction_fee=0,
            total_storage_fee=0,
            statistics=[(1, 1, 0, 0)],
            transactions_hash=None,
            receipts_hash=None,
            difficulty=1,
            validator_public_key_bytes=None,
            nonce=0,
        )

        nonce = generate_block_nonce(block=block, difficulty=1)
        self.assertEqual(block.nonce, nonce)
        self.assertGreaterEqual(nonce, 0)
        self.assertIsNotNone(block.expr_id)

        leading_zeros = count_leading_zero_bits(block.expr_id)
        self.assertGreaterEqual(leading_zeros, 1)
