"""A block whose hash cells were resolved in place decodes to the same block.

``resolve_inner_exprs`` fills in the child of a cell when it can fetch it and
leaves ``head_hash`` as ``None``. A node that does this to its own genesis
(it can when a peer already holds the accounts) stores cells in that form.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.encoding.decode import get_block_from_storage
from astreum.consensus.block.encoding.expr import get_block_expr
from astreum.expression import ZERO32, bytes_, resolve_inner_exprs
from astreum.node import Node
from astreum.storage.exprs import put_expr_in_hot_storage
from tests.unit.consensus.block.test_expr import _make_block


class TestDecodeResolvedCells(unittest.TestCase):
    def setUp(self) -> None:
        self.node = Node(config={"default_seed": None, "verbose": False})

    def _store(self, block, referenced):
        for expr in referenced:
            put_expr_in_hot_storage(self.node, expr)
        block_expr = get_block_expr(block)
        exprs, _ = resolve_inner_exprs(self.node, block_expr)
        for expr in exprs:
            put_expr_in_hot_storage(self.node, expr)
        return block_expr.hash()

    def test_hashes_survive_when_the_referenced_exprs_are_stored(self) -> None:
        accounts, previous, txs, receipts = (bytes_(name) for name in (b"acc", b"prev", b"txs", b"rcp"))
        bloom, era = bytes_(b"bloom"), bytes_(b"era")
        block = _make_block(
            previous_block_hash=previous.hash(),
            accounts_hash=accounts.hash(),
            transactions_hash=txs.hash(),
            receipts_hash=receipts.hash(),
            bloom_hash=bloom.hash(),
            previous_era_hash=era.hash(),
        )
        block_id = self._store(block, [accounts, previous, txs, receipts, bloom, era])

        decoded = get_block_from_storage(astreum_node=self.node, block_hash=block_id)

        self.assertEqual(decoded.accounts_hash, accounts.hash())
        self.assertEqual(decoded.previous_block_hash, previous.hash())
        self.assertEqual(decoded.transactions_hash, txs.hash())
        self.assertEqual(decoded.receipts_hash, receipts.hash())
        self.assertEqual(decoded.bloom_hash, bloom.hash())
        self.assertEqual(decoded.previous_era_hash, era.hash())
        self.assertEqual(get_block_expr(decoded).hash(), block_id)

    def test_zero_hashes_stay_zero(self) -> None:
        block = _make_block(transactions_hash=ZERO32, receipts_hash=ZERO32)
        block_id = self._store(block, [])
        decoded = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(decoded.transactions_hash, ZERO32)
        self.assertEqual(decoded.receipts_hash, ZERO32)


if __name__ == "__main__":
    unittest.main()
