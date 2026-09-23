import sys
import unittest
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.transaction.treasury.record import (
    TreasuryCreditOffer,
    TreasuryUserRecord,
)
from astreum.expression import Expr, ZERO32, resolve_list_exprs


class _FakeNode:
    def __init__(self):
        self.hot_storage = {}
        self.hot_storage_lock = threading.Lock()
        self.hot_storage_timestamps = {}
        self.hot_storage_size = 0
        self.config = {
            "expr_fetch_interval": 0,
            "expr_fetch_retries": 0,
            "hot_storage_limit": 10 * 1024 * 1024,
            "cold_storage_path": None,
        }
        self.is_connected = False
        self.logger = type(
            "L", (), {"debug": lambda *a, **kw: None, "info": lambda *a, **kw: None}
        )()

    def get_expr(self, head_hash: bytes):
        return self.hot_storage.get(head_hash)


class TestTreasuryRecord(unittest.TestCase):
    def test_to_expr_uses_expected_field_order(self):
        loans_root_hash = b"\x01" * 32
        offers_root_hash = b"\x02" * 32
        record = TreasuryUserRecord(
            balance=7,
            loans_root_hash=loans_root_hash,
            total_interest_paid=3,
            offers_root_hash=offers_root_hash,
        )

        expr = record.expr()
        self.assertIsNotNone(expr.hash())

        nodes, missed = resolve_list_exprs(_FakeNode(), expr)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 4)

        self.assertEqual(nodes[0]._tag, "int")
        self.assertEqual(nodes[0].value, 7)  # balance

        self.assertEqual(nodes[1]._tag, "link")
        self.assertEqual(nodes[1]._head_hash, loans_root_hash)  # loans_root_hash ref

        self.assertEqual(nodes[2]._tag, "int")
        self.assertEqual(nodes[2].value, 3)  # total_interest_paid

        self.assertEqual(nodes[3]._tag, "link")
        self.assertEqual(nodes[3]._head_hash, offers_root_hash)  # offers_root_hash ref

    def test_from_storage_round_trip(self):
        node = _FakeNode()
        record = TreasuryUserRecord(
            balance=42,
            loans_root_hash=b"\x03" * 32,
            total_interest_paid=9,
            offers_root_hash=b"\x04" * 32,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryUserRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.balance, 42)
        self.assertEqual(loaded.loans_root_hash, b"\x03" * 32)
        self.assertEqual(loaded.total_interest_paid, 9)
        self.assertEqual(loaded.offers_root_hash, b"\x04" * 32)

    def test_from_storage_rejects_wrong_field_count(self):
        # A 3-field record (pre-extension shape) must not be misread as valid.
        node = _FakeNode()
        old_shape = TreasuryUserRecord(balance=1, loans_root_hash=ZERO32, total_interest_paid=0)
        # Manually build the legacy 3-field expr (without offers_root_hash).
        from astreum.expression import NIL, int_, link

        legacy_expr = link(int_(0), NIL)
        legacy_expr = link(Expr("link", head_hash=ZERO32), legacy_expr)
        legacy_expr = link(int_(1), legacy_expr)
        node.hot_storage[legacy_expr.hash()] = legacy_expr

        loaded = TreasuryUserRecord.from_storage(node, legacy_expr.hash())
        self.assertIsNone(loaded)


class TestTreasuryCreditOffer(unittest.TestCase):
    def test_to_expr_uses_expected_field_order(self):
        offer = TreasuryCreditOffer(
            limit=1000,
            duration=8,
            price=50,
            expiry=100,
        )
        expr = offer.expr()
        nodes, missed = resolve_list_exprs(_FakeNode(), expr)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 5)

        self.assertEqual(nodes[0].value, 1000)  # limit
        self.assertEqual(nodes[1].value, 8)  # duration
        self.assertEqual(nodes[2].value, 50)  # price
        self.assertEqual(nodes[3].value, 100)  # expiry
        self.assertEqual(nodes[4]._tag, "link")
        self.assertEqual(nodes[4]._head_hash, ZERO32)  # claimed_by (unclaimed)

    def test_round_trip_unclaimed(self):
        node = _FakeNode()
        offer = TreasuryCreditOffer(limit=10, duration=4, price=1, expiry=50)
        expr = offer.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryCreditOffer.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.limit, 10)
        self.assertEqual(loaded.duration, 4)
        self.assertEqual(loaded.price, 1)
        self.assertEqual(loaded.expiry, 50)
        self.assertEqual(loaded.claimed_by, ZERO32)

    def test_round_trip_claimed(self):
        node = _FakeNode()
        claimant_id = b"\x09" * 32
        offer = TreasuryCreditOffer(
            limit=10, duration=4, price=1, expiry=50, claimed_by=claimant_id,
        )
        expr = offer.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryCreditOffer.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.claimed_by, claimant_id)


if __name__ == "__main__":
    unittest.main()
