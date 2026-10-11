import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.block import Block  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.expression import ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.storage.exprs import put_expr_in_hot_storage  # noqa: E402
from astreum.storage.cold import put_expr_in_cold_storage  # noqa: E402


def _make_block(**overrides):
    kwargs = dict(
        chain_id=0,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=1,
        timestamp=1234567890,
        accounts_hash=b"a" * 32,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, 1, 0, 0)],
        transactions_hash=b"t" * 32,
        receipts_hash=b"r" * 32,
        difficulty=1,
        validator_public_key_bytes=b"v" * 32,
        signature=b"sig",
        accounts=None,
        transactions=None,
        receipts=None,
    )
    kwargs.update(overrides)
    return create_block(**kwargs)


def _assert_block_fields(test_case: unittest.TestCase, b: Block):
    test_case.assertEqual(b.previous_block_hash, ZERO32)
    test_case.assertIsNone(b.previous_block)
    test_case.assertEqual(b.height, 1)
    test_case.assertEqual(b.timestamp, 1234567890)
    test_case.assertEqual(b.accounts_hash, b"a" * 32)
    test_case.assertEqual(b.total_transaction_fee, 0)
    test_case.assertEqual(b.total_storage_fee, 0)
    test_case.assertEqual(b.total_fee, 0)
    test_case.assertEqual(b.statistics, [(1, 1, 0, 0)])
    test_case.assertEqual(b.cumulative_total_fee, 1)
    test_case.assertEqual(b.cumulative_stake, 1)
    test_case.assertEqual(b.transactions_hash, b"t" * 32)
    test_case.assertEqual(b.receipts_hash, b"r" * 32)
    test_case.assertEqual(b.difficulty, 1)
    test_case.assertEqual(b.validator_public_key_bytes, b"v" * 32)
    test_case.assertEqual(b.signature, b"sig")
    test_case.assertIsInstance(b.body_hash, (bytes, bytearray))
    test_case.assertTrue(b.body_hash)


class TestBlockExpr(unittest.TestCase):
    def setUp(self):
        self.node = Node(config={})

    def test_block_to_from_expr_roundtrip_hot_storage(self):
        b = _make_block()
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(get_block_expr(b2).hash(), block_id)
        _assert_block_fields(self, b2)


class TestBlockExprColdStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = Node(config={
            "cold_storage_path": self.temp_dir.name,
            "cold_storage_scale": "KB",
            "default_seed": None,
            "verbose": False,
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_block_to_from_expr_roundtrip_cold_storage(self):
        b = _make_block()
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(get_block_expr(b2).hash(), block_id)
        _assert_block_fields(self, b2)

    def test_block_roundtrip_nonzero_chain_id(self):
        b = _make_block(chain_id=7)
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.chain_id, 7)

    def test_block_roundtrip_different_height_difficulty(self):
        b = _make_block(height=42, difficulty=99)
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.height, 42)
        self.assertEqual(b2.difficulty, 99)


if __name__ == "__main__":
    unittest.main(verbosity=2)
