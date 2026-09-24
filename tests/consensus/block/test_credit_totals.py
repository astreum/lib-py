"""Tests for the block header's global credit totals
(`global_loaned`/`global_defaulted`/`global_loan_count`).

These three cumulative counters live in the block body (encode/decode) and
default to 0 when unset, matching genesis and every pre-existing block
constructor call in the codebase.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

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


class TestCreditTotals(unittest.TestCase):
    def setUp(self):
        self.node = Node(config={})

    def test_defaults_to_zero(self):
        b = _make_block()
        self.assertEqual(b.global_loaned, 0)
        self.assertEqual(b.global_defaulted, 0)
        self.assertEqual(b.global_loan_count, 0)

    def test_round_trip_nonzero_totals(self):
        b = _make_block(global_loaned=12_345, global_defaulted=678, global_loan_count=9)
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(get_block_expr(b2).hash(), block_id)
        self.assertEqual(b2.global_loaned, 12_345)
        self.assertEqual(b2.global_defaulted, 678)
        self.assertEqual(b2.global_loan_count, 9)

    def test_round_trip_zero_totals(self):
        b = _make_block()
        block_id = get_block_expr(b).hash()
        inner_exprs, _ = resolve_inner_exprs(self.node, get_block_expr(b))
        for e in inner_exprs:
            put_expr_in_hot_storage(self.node, e)

        b2 = get_block_from_storage(astreum_node=self.node, block_hash=block_id)
        self.assertEqual(b2.global_loaned, 0)
        self.assertEqual(b2.global_defaulted, 0)
        self.assertEqual(b2.global_loan_count, 0)

    def test_snapshot_restore_round_trips_totals(self):
        from astreum.consensus.models.accounts import Accounts

        b = _make_block(accounts=Accounts(), global_loaned=100, global_defaulted=10, global_loan_count=1)
        snapshot = b.snapshot()
        b.global_loaned += 50
        b.global_defaulted += 5
        b.global_loan_count += 1

        b.restore(snapshot)
        self.assertEqual(b.global_loaned, 100)
        self.assertEqual(b.global_defaulted, 10)
        self.assertEqual(b.global_loan_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
