"""Tests for the loan counters inside the block statistics.

``total_loan_count``, ``total_loaned``, and ``total_written_off`` live in the
statistics entry (encode/decode). A block with no statistics holds ``None``.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.statistics import BlockStatistics  # noqa: E402

from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.expression import ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.storage.exprs import put_expr_in_hot_storage  # noqa: E402


def _make_block(**overrides):
    kwargs = dict(
        chain_id=0,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=1,
        timestamp=1234567890,
        accounts_hash=b"a" * 32,
        total_transaction_fee=0,
        total_base_fee=0,
        transaction_count=0,
        total_storage_fee=0,
        statistics=BlockStatistics(1, 1, 1, 0, 0, 0, 0, 0, 0),
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


class TestCreditTotals(unittest.TestCase):
    def setUp(self):
        self.node = Node(config={})

    def test_a_block_without_statistics_holds_none(self):
        b = _make_block(statistics=None)
        self.assertIsNone(b.statistics)

    def test_the_counters_start_at_zero(self):
        b = _make_block()
        self.assertEqual(b.statistics.total_loaned, 0)
        self.assertEqual(b.statistics.total_written_off, 0)
        self.assertEqual(b.statistics.total_loan_count, 0)

    def test_round_trip_nonzero_totals(self):
        b = _make_block(statistics=BlockStatistics(1, 1, 1, 0, 0, 0, 9, 12_345, 678))
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(get_block_expr(b2).hash(), block_id)
        self.assertEqual(b2.statistics.total_loaned, 12_345)
        self.assertEqual(b2.statistics.total_written_off, 678)
        self.assertEqual(b2.statistics.total_loan_count, 9)

    def test_a_block_with_no_statistics_decodes_to_none(self):
        b = _make_block(statistics=None)
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertIsNone(b2.statistics)
        self.assertEqual(b2.total_balance, 0)

    def test_round_trip_zero_totals(self):
        b = _make_block()
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.statistics.total_loaned, 0)
        self.assertEqual(b2.statistics.total_written_off, 0)
        self.assertEqual(b2.statistics.total_loan_count, 0)

    def test_snapshot_restore_round_trips_totals(self):
        from astreum.consensus.models.accounts import Accounts

        b = _make_block(
            accounts=Accounts(),
            statistics=BlockStatistics(1, 1, 1, 0, 0, 0, 1, 100, 10),
        )
        snapshot = b.snapshot()
        b.statistics = b.statistics.with_loan(50).with_write_off(5)

        b.restore(snapshot)
        self.assertEqual(b.statistics.total_loan_count, 1)
        self.assertEqual(b.statistics.total_loaned, 100)
        self.assertEqual(b.statistics.total_written_off, 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
