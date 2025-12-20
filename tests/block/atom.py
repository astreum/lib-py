import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.validation.models.block import Block  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.models.atom import ZERO32  # noqa: E402


class TestBlockAtom(unittest.TestCase):
    def setUp(self):
        # Minimal node with in-memory storage
        self.node = Node(config={})

    def test_block_to_from_atom_roundtrip(self):
        # Create a block with required fields
        b = Block(
            chain_id=0,
            previous_block_hash=ZERO32,
            previous_block=None,
            number=1,
            timestamp=1234567890,
            accounts_hash=b"a" * 32,
            transactions_total_fees=0,
            transactions_hash=b"t" * 32,
            receipts_hash=b"r" * 32,
            delay_difficulty=1,
            delay_output=b"out",
            validator_public_key=b"v" * 32,
            signature=b"sig",
            accounts=None,
            transactions=None,
            receipts=None,
        )

        # Serialize to atoms and persist in node storage
        block_id, atoms = b.to_atom()
        for a in atoms:
            self.node._local_set(a.object_id(), a)

        # Retrieve from storage and validate fields
        b2 = Block.from_atom(self.node, block_id)
        self.assertEqual(b2.hash, block_id)
        self.assertEqual(b2.previous_block_hash, ZERO32)
        self.assertIsNone(b2.previous_block)
        self.assertEqual(b2.number, 1)
        self.assertEqual(b2.timestamp, 1234567890)
        self.assertEqual(b2.accounts_hash, b"a" * 32)
        self.assertEqual(b2.transactions_total_fees, 0)
        self.assertEqual(b2.transactions_hash, b"t" * 32)
        self.assertEqual(b2.receipts_hash, b"r" * 32)
        self.assertEqual(b2.delay_difficulty, 1)
        self.assertEqual(b2.delay_output, b"out")
        self.assertEqual(b2.validator_public_key, b"v" * 32)
        self.assertEqual(b2.signature, b"sig")
        self.assertIsNone(b2.accounts)
        self.assertIsNone(b2.transactions)
        # Body hash present
        self.assertIsInstance(b2.body_hash, (bytes, bytearray))
        self.assertTrue(b2.body_hash)


if __name__ == "__main__":
    unittest.main(verbosity=2)
