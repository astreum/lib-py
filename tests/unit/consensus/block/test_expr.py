import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.block import Block  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.expression import (  # noqa: E402
    NIL,
    ZERO32,
    bytes_,
    int_,
    link,
    resolve_inner_exprs,
    resolve_list_exprs,
    symbol,
)
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
        statistics=[(1, 1, 0, 0, 0, 0)],
        transactions_hash=b"t" * 32,
        receipts_hash=b"r" * 32,
        validation_difficulty=1,
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
    test_case.assertEqual(b.statistics, [(1, 1, 0, 0, 0, 0)])
    test_case.assertEqual(b.cumulative_total_fee, 1)
    test_case.assertEqual(b.cumulative_stake, 1)
    test_case.assertEqual(b.transactions_hash, b"t" * 32)
    test_case.assertEqual(b.receipts_hash, b"r" * 32)
    test_case.assertEqual(b.validation_difficulty, 1)
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
        b = _make_block(height=42, validation_difficulty=99)
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.height, 42)
        self.assertEqual(b2.validation_difficulty, 99)

    def test_block_roundtrip_storage_difficulty(self):
        b = _make_block(storage_difficulty=7)
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.storage_difficulty, 7)
        self.assertEqual(get_block_expr(b2).hash(), block_id)

    def test_statistics_roundtrip_claims(self):
        stats = [(10, 20, 3, 0, 0, 0), (1, 2, 3, 4, 5, 6), (7, 8, 9, 10, 11, 12)]
        b = _make_block(statistics=stats)
        block_expr = get_block_expr(b)
        block_id = block_expr.hash()
        put_expr_in_cold_storage(self.node, block_expr)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.statistics, stats)
        self.assertEqual(get_block_expr(b2).hash(), block_id)

    def test_decode_rejects_18_field_body(self):
        b = _make_block()
        body = get_block_expr(b).head.head
        nodes, _ = resolve_list_exprs(self.node, body)
        short_body = NIL
        for node in reversed(nodes[:12] + nodes[13:]):
            short_body = link(node, short_body)
        short_expr = link(link(short_body, link(bytes_(b.signature), NIL)), symbol("block"))
        put_expr_in_cold_storage(self.node, short_expr)

        with self.assertRaisesRegex(ValueError, "malformed block body length"):
            get_block_from_storage(astreum_node=self.node, block_hash=short_expr.hash())

    def _put_with_statistics(self, entry_sizes):
        b = _make_block()
        body = get_block_expr(b).head.head
        nodes, _ = resolve_list_exprs(self.node, body)
        stats = NIL
        for size in reversed(entry_sizes):
            entry = NIL
            for value in reversed(range(1, size + 1)):
                entry = link(int_(value), entry)
            stats = link(entry, stats)
        bad_body = NIL
        for node in reversed(nodes[:18] + [stats]):
            bad_body = link(node, bad_body)
        bad_expr = link(link(bad_body, link(bytes_(b.signature), NIL)), symbol("block"))
        put_expr_in_cold_storage(self.node, bad_expr)
        return bad_expr.hash()

    def test_decode_accepts_wire_statistics_sizes(self):
        block_hash = self._put_with_statistics([3, 6])
        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_hash)
        self.assertEqual(b2.statistics, [(1, 2, 3, 0, 0, 0), (1, 2, 3, 4, 5, 6)])

    def test_decode_rejects_statistics_entry_0_with_wrong_count(self):
        for size in (2, 4, 6):
            with self.subTest(size=size):
                block_hash = self._put_with_statistics([size])
                with self.assertRaisesRegex(ValueError, "invalid statistics entry 0 length"):
                    get_block_from_storage(astreum_node=self.node, block_hash=block_hash)

    def test_decode_rejects_statistics_entry_n_with_wrong_count(self):
        for size in (3, 4, 5):
            with self.subTest(size=size):
                block_hash = self._put_with_statistics([3, size])
                with self.assertRaisesRegex(ValueError, "invalid statistics entry 1 length"):
                    get_block_from_storage(astreum_node=self.node, block_hash=block_hash)

    def _put_with_statistics(self, entry_sizes):
        b = _make_block()
        body = get_block_expr(b).head.head
        nodes, _ = resolve_list_exprs(self.node, body)
        stats = NIL
        for size in reversed(entry_sizes):
            entry = NIL
            for value in reversed(range(1, size + 1)):
                entry = link(int_(value), entry)
            stats = link(entry, stats)
        bad_body = NIL
        for node in reversed(nodes[:18] + [stats]):
            bad_body = link(node, bad_body)
        bad_expr = link(link(bad_body, link(bytes_(b.signature), NIL)), symbol("block"))
        put_expr_in_cold_storage(self.node, bad_expr)
        return bad_expr.hash()

    def test_decode_accepts_wire_statistics_sizes(self):
        block_hash = self._put_with_statistics([3, 6])
        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_hash)
        self.assertEqual(b2.statistics, [(1, 2, 3, 0, 0, 0), (1, 2, 3, 4, 5, 6)])

    def test_decode_rejects_statistics_entry_0_with_wrong_count(self):
        for size in (2, 4, 6):
            with self.subTest(size=size):
                block_hash = self._put_with_statistics([size])
                with self.assertRaisesRegex(ValueError, "invalid statistics entry 0 length"):
                    get_block_from_storage(astreum_node=self.node, block_hash=block_hash)

    def test_decode_rejects_statistics_entry_n_with_wrong_count(self):
        for size in (3, 4, 5):
            with self.subTest(size=size):
                block_hash = self._put_with_statistics([3, size])
                with self.assertRaisesRegex(ValueError, "invalid statistics entry 1 length"):
                    get_block_from_storage(astreum_node=self.node, block_hash=block_hash)


class TestBlockCounters(unittest.TestCase):
    def test_block_body_has_19_fields(self):
        b = _make_block()
        body = get_block_expr(b).head.head
        nodes, missed = resolve_list_exprs(None, body)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 19)

    def test_storage_difficulty_defaults_to_zero(self):
        b = _make_block()
        self.assertEqual(b.storage_difficulty, 0)
        self.assertEqual(b.storage_votes, 0)
        self.assertEqual(b.storage_claim_count, 0)

    def test_snapshot_restores_storage_counters(self):
        b = _make_block()
        b.accounts = Accounts()
        b.storage_votes = 2
        b.storage_claim_count = 3
        snap = b.snapshot()
        b.storage_votes = 5
        b.storage_claim_count = 9
        b.restore(snap)
        self.assertEqual(b.storage_votes, 2)
        self.assertEqual(b.storage_claim_count, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
