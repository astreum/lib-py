import sys
import unittest
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryCreditOffer,
    TreasuryLoanRecord,
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
            loaned=11,
            defaulted=5,
            sold_limit=13,
        )

        expr = record.expr()
        self.assertIsNotNone(expr.hash())

        nodes, missed = resolve_list_exprs(_FakeNode(), expr)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 7)

        self.assertEqual(nodes[0]._tag, "int")
        self.assertEqual(nodes[0].value, 7)  # balance

        self.assertEqual(nodes[1]._tag, "link")
        self.assertEqual(nodes[1]._head_hash, loans_root_hash)  # loans_root_hash ref

        self.assertEqual(nodes[2]._tag, "int")
        self.assertEqual(nodes[2].value, 3)  # total_interest_paid

        self.assertEqual(nodes[3]._tag, "link")
        self.assertEqual(nodes[3]._head_hash, offers_root_hash)  # offers_root_hash ref

        self.assertEqual(nodes[4]._tag, "int")
        self.assertEqual(nodes[4].value, 11)  # loaned

        self.assertEqual(nodes[5]._tag, "int")
        self.assertEqual(nodes[5].value, 5)  # defaulted

        self.assertEqual(nodes[6]._tag, "int")
        self.assertEqual(nodes[6].value, 13)  # sold_limit

    def test_from_storage_round_trip(self):
        node = _FakeNode()
        record = TreasuryUserRecord(
            balance=42,
            loans_root_hash=b"\x03" * 32,
            total_interest_paid=9,
            offers_root_hash=b"\x04" * 32,
            loaned=100,
            defaulted=20,
            sold_limit=30,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryUserRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.balance, 42)
        self.assertEqual(loaded.loans_root_hash, b"\x03" * 32)
        self.assertEqual(loaded.total_interest_paid, 9)
        self.assertEqual(loaded.offers_root_hash, b"\x04" * 32)
        self.assertEqual(loaded.loaned, 100)
        self.assertEqual(loaded.defaulted, 20)
        self.assertEqual(loaded.sold_limit, 30)

    def test_from_storage_accepts_legacy_four_field_shape(self):
        # A record written before loaned/defaulted/sold_limit existed must
        # still decode, defaulting the new fields to 0.
        node = _FakeNode()
        from astreum.expression import NIL, int_, link

        legacy_expr = link(Expr("link", head_hash=b"\x04" * 32), NIL)
        legacy_expr = link(int_(9), legacy_expr)
        legacy_expr = link(Expr("link", head_hash=b"\x03" * 32), legacy_expr)
        legacy_expr = link(int_(42), legacy_expr)
        node.hot_storage[legacy_expr.hash()] = legacy_expr

        loaded = TreasuryUserRecord.from_storage(node, legacy_expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.balance, 42)
        self.assertEqual(loaded.loans_root_hash, b"\x03" * 32)
        self.assertEqual(loaded.total_interest_paid, 9)
        self.assertEqual(loaded.offers_root_hash, b"\x04" * 32)
        self.assertEqual(loaded.loaned, 0)
        self.assertEqual(loaded.defaulted, 0)
        self.assertEqual(loaded.sold_limit, 0)

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


class TestTreasuryLoanRecord(unittest.TestCase):
    def _base_kwargs(self):
        return dict(
            creation_block_number=10,
            loan_type=LoanType.UNSECURED,
            discounted_amount=900,
            payment_amount=100,
            payment_interval_blocks=5,
            next_payment_block_number=15,
            payment_count=10,
        )

    def test_round_trip_with_claimed_offers(self):
        node = _FakeNode()
        seller_a = b"\x11" * 32
        seller_b = b"\x22" * 32
        offer_a = b"\x33" * 32
        offer_b = b"\x44" * 32
        record = TreasuryLoanRecord(
            **self._base_kwargs(),
            claimed_offers=[(seller_a, offer_a, 500), (seller_b, offer_b, 400)],
            insurance_fee=17,
            missed_count=0,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.creation_block_number, 10)
        self.assertEqual(loaded.loan_type, LoanType.UNSECURED)
        self.assertEqual(loaded.discounted_amount, 900)
        self.assertEqual(loaded.payment_amount, 100)
        self.assertEqual(loaded.payment_interval_blocks, 5)
        self.assertEqual(loaded.next_payment_block_number, 15)
        self.assertEqual(loaded.payment_count, 10)
        self.assertEqual(
            loaded.claimed_offers,
            [(seller_a, offer_a, 500), (seller_b, offer_b, 400)],
        )
        self.assertEqual(loaded.insurance_fee, 17)
        self.assertEqual(loaded.missed_count, 0)

    def test_round_trip_empty_claimed_offers(self):
        node = _FakeNode()
        record = TreasuryLoanRecord(
            **{**self._base_kwargs(), "loan_type": LoanType.SECURED},
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.claimed_offers, [])
        self.assertEqual(loaded.insurance_fee, 0)
        self.assertEqual(loaded.missed_count, 0)

    def test_from_storage_accepts_legacy_seven_field_shape(self):
        node = _FakeNode()
        from astreum.expression import NIL, int_, link

        legacy_expr = link(int_(5), NIL)  # payment_interval_blocks
        legacy_expr = link(int_(100), legacy_expr)  # payment_amount
        legacy_expr = link(int_(15), legacy_expr)  # next_payment_block_number
        legacy_expr = link(int_(int(LoanType.SECURED)), legacy_expr)  # loan_type
        legacy_expr = link(int_(10), legacy_expr)  # payment_count
        legacy_expr = link(int_(900), legacy_expr)  # discounted_amount
        legacy_expr = link(int_(10), legacy_expr)  # creation_block_number
        node.hot_storage[legacy_expr.hash()] = legacy_expr

        loaded = TreasuryLoanRecord.from_storage(node, legacy_expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.claimed_offers, [])
        self.assertEqual(loaded.insurance_fee, 0)
        self.assertEqual(loaded.missed_count, 0)


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
