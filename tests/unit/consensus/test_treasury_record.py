import dataclasses
import sys
import unittest
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.transaction.treasury.record import (
    BORROW_REQUEST_VERSION,
    LoanType,
    TreasuryBorrowRequest,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    decode_borrow_request,
    encode_borrow_request,
)
from astreum.expression import resolve_list_exprs
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.expression.helpers import get_expr_builtin_tag
from astreum.utils.config import NodeConfig


class _FakeNode:
    def __init__(self):
        self.hot_storage = {}
        self.hot_storage_lock = threading.Lock()
        self.hot_storage_timestamps = {}
        self.hot_storage_size = 0
        self.config = NodeConfig(
            storage_fetch_interval=0,
            storage_fetch_retries=0,
            hot_storage_limit=10 * 1024 * 1024,
            cold_storage_path=None,
        )
        self.is_connected = False
        self.logger = type(
            "L", (), {"debug": lambda *a, **kw: None, "info": lambda *a, **kw: None}
        )()

    def get_expr(self, head_hash: bytes):
        return self.hot_storage.get(head_hash)


class TestTreasuryRecord(unittest.TestCase):
    def test_a_replaced_record_encodes_its_own_fields(self):
        record = TreasuryUserRecord(balance=500)
        record.expr()
        updated = replace(record, balance=250)
        self.assertEqual(updated.expr().hash(), TreasuryUserRecord(balance=250).expr().hash())
        self.assertNotEqual(updated.expr().hash(), record.expr().hash())

    def test_to_expr_uses_expected_field_order(self):
        loans_root_hash = b"\x01" * 32
        record = TreasuryUserRecord(
            balance=7,
            loans_root_hash=loans_root_hash,
            total_interest_paid=3,
            loaned=11,
            defaulted=5,
        )

        expr = record.expr()
        self.assertIsNotNone(expr.hash())

        nodes, missed = resolve_list_exprs(_FakeNode(), expr)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 5)

        self.assertEqual(get_expr_builtin_tag(nodes[0]), "int")
        self.assertEqual(nodes[0].value, 7)  # balance

        self.assertEqual(get_expr_builtin_tag(nodes[1]), "link")
        self.assertEqual(nodes[1].head_hash, loans_root_hash)  # loans_root_hash ref

        self.assertEqual(get_expr_builtin_tag(nodes[2]), "int")
        self.assertEqual(nodes[2].value, 3)  # total_interest_paid

        self.assertEqual(get_expr_builtin_tag(nodes[3]), "int")
        self.assertEqual(nodes[3].value, 11)  # loaned

        self.assertEqual(get_expr_builtin_tag(nodes[4]), "int")
        self.assertEqual(nodes[4].value, 5)  # defaulted

    def test_from_storage_round_trip(self):
        node = _FakeNode()
        record = TreasuryUserRecord(
            balance=42,
            loans_root_hash=b"\x03" * 32,
            total_interest_paid=9,
            loaned=100,
            defaulted=20,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryUserRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.balance, 42)
        self.assertEqual(loaded.loans_root_hash, b"\x03" * 32)
        self.assertEqual(loaded.total_interest_paid, 9)
        self.assertEqual(loaded.loaned, 100)
        self.assertEqual(loaded.defaulted, 20)

    def test_from_storage_rejects_the_old_seven_field_shape(self):
        # The deleted guarantee trie and guaranteed capacity are not decoded:
        # the chain restarts, so no guarantee-bearing record can exist.
        node = _FakeNode()
        from astreum.expression import NIL, int_, link

        legacy_expr = link(int_(5), NIL)  # guaranteed
        legacy_expr = link(int_(4), legacy_expr)  # defaulted
        legacy_expr = link(int_(3), legacy_expr)  # loaned
        legacy_expr = link(link(int_(0), NIL), legacy_expr)  # guarantees root
        legacy_expr = link(int_(7), legacy_expr)  # total_interest_paid
        legacy_expr = link(link(int_(0), NIL), legacy_expr)  # loans root
        legacy_expr = link(int_(500), legacy_expr)  # balance
        node.hot_storage[legacy_expr.hash()] = legacy_expr

        self.assertIsNone(TreasuryUserRecord.from_storage(node, legacy_expr.hash()))

    def test_from_storage_rejects_wrong_field_count(self):
        # A 3-field record (the shape before the loans root was paired with
        # the counters) is not decoded.
        node = _FakeNode()
        from astreum.expression import NIL, int_, link

        legacy_expr = link(int_(0), NIL)
        legacy_expr = link(link(int_(0), NIL), legacy_expr)
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
            paid_until_block=10,
        )

    def test_field_set_is_the_perpetual_shape(self):
        field_names = [
            f.name for f in dataclasses.fields(TreasuryLoanRecord) if not f.name.startswith("_")
        ]
        self.assertEqual(
            field_names,
            [
                "creation_block_number",
                "loan_type",
                "discounted_amount",
                "payment_amount",
                "paid_until_block",
                "insurance_interval_fee",
                "missed_count",
                "owner",
            ],
        )

    def test_to_expr_uses_expected_field_order(self):
        record = TreasuryLoanRecord(
            **self._base_kwargs(),
            insurance_interval_fee=17,
            missed_count=3,
            owner=b"\xaa" * 32,
        )
        nodes, missed = resolve_list_exprs(_FakeNode(), record.expr())
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 8)

        self.assertEqual(nodes[0].value, 10)  # creation_block_number
        self.assertEqual(nodes[1].value, int(LoanType.UNSECURED))  # loan_type
        self.assertEqual(nodes[2].value, 900)  # discounted_amount
        self.assertEqual(nodes[3].value, 100)  # payment_amount
        self.assertEqual(nodes[4].value, 10)  # paid_until_block
        self.assertEqual(nodes[5].value, 17)  # insurance_interval_fee
        self.assertEqual(nodes[6].value, 3)  # missed_count
        self.assertEqual(get_expr_builtin_tag(nodes[7]), "bytes")  # owner
        self.assertEqual(nodes[7].value, b"\xaa" * 32)

    def test_round_trip(self):
        node = _FakeNode()
        custom_owner = b"\xaa" * 32
        record = TreasuryLoanRecord(
            **self._base_kwargs(),
            insurance_interval_fee=17,
            missed_count=3,
            owner=custom_owner,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.creation_block_number, 10)
        self.assertEqual(loaded.loan_type, LoanType.UNSECURED)
        self.assertEqual(loaded.discounted_amount, 900)
        self.assertEqual(loaded.payment_amount, 100)
        self.assertEqual(loaded.paid_until_block, 10)
        self.assertEqual(loaded.insurance_interval_fee, 17)
        self.assertEqual(loaded.missed_count, 3)
        self.assertEqual(loaded.owner, custom_owner)

    def test_round_trip_defaults(self):
        node = _FakeNode()
        record = TreasuryLoanRecord(
            **{**self._base_kwargs(), "loan_type": LoanType.SECURED},
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.insurance_interval_fee, 0)
        self.assertEqual(loaded.missed_count, 0)
        self.assertEqual(loaded.owner, TREASURY_ADDRESS)

    def test_loan_type_values_are_stable(self):
        self.assertEqual(int(LoanType.SECURED), 0)
        self.assertEqual(int(LoanType.UNSECURED), 1)
        self.assertEqual(int(LoanType.CLAIM_COST), 2)

    def test_round_trip_claim_cost_loan(self):
        node = _FakeNode()
        record = TreasuryLoanRecord(
            creation_block_number=42,
            loan_type=LoanType.CLAIM_COST,
            discounted_amount=1_200,
            payment_amount=6,
            paid_until_block=42,
        )
        expr = record.expr()
        node.hot_storage[expr.hash()] = expr

        nodes, missed = resolve_list_exprs(node, expr)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 8)
        self.assertEqual(nodes[1].value, int(LoanType.CLAIM_COST))

        loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.creation_block_number, 42)
        self.assertEqual(loaded.loan_type, LoanType.CLAIM_COST)
        self.assertEqual(loaded.discounted_amount, 1_200)
        self.assertEqual(loaded.payment_amount, 6)
        self.assertEqual(loaded.paid_until_block, 42)
        self.assertEqual(loaded.insurance_interval_fee, 0)
        self.assertEqual(loaded.missed_count, 0)
        self.assertEqual(loaded.owner, TREASURY_ADDRESS)

    def test_existing_loan_types_round_trip_unchanged(self):
        for loan_type in (LoanType.SECURED, LoanType.UNSECURED):
            with self.subTest(loan_type=loan_type):
                node = _FakeNode()
                record = TreasuryLoanRecord(
                    **{**self._base_kwargs(), "loan_type": loan_type},
                )
                expr = record.expr()
                node.hot_storage[expr.hash()] = expr

                loaded = TreasuryLoanRecord.from_storage(node, expr.hash())
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.loan_type, loan_type)

    def test_from_storage_rejects_legacy_shapes(self):
        # The old fixed-term records are not decoded: the chain restarts at
        # genesis, so no old-shape record can exist on chain.
        node = _FakeNode()
        from astreum.expression import NIL, bytes_, int_, link

        legacy_eleven = link(bytes_(b"\xaa" * 32), NIL)  # owner
        legacy_eleven = link(int_(0), legacy_eleven)  # missed_count
        legacy_eleven = link(int_(17), legacy_eleven)  # insurance_interval_fee
        legacy_eleven = link(link(int_(500), NIL), legacy_eleven)  # claimed
        legacy_eleven = link(int_(5), legacy_eleven)  # payment_interval_blocks
        legacy_eleven = link(int_(100), legacy_eleven)  # payment_amount
        legacy_eleven = link(int_(15), legacy_eleven)  # next_payment_block_number
        legacy_eleven = link(int_(int(LoanType.SECURED)), legacy_eleven)
        legacy_eleven = link(int_(10), legacy_eleven)  # payment_count
        legacy_eleven = link(int_(900), legacy_eleven)  # discounted_amount
        legacy_eleven = link(int_(10), legacy_eleven)  # creation_block_number
        node.hot_storage[legacy_eleven.hash()] = legacy_eleven

        loaded = TreasuryLoanRecord.from_storage(node, legacy_eleven.hash())
        self.assertIsNone(loaded)

        legacy_ten = NIL
        legacy_ten = link(int_(0), legacy_ten)  # missed_count
        legacy_ten = link(int_(17), legacy_ten)  # insurance_interval_fee
        legacy_ten = link(link(int_(500), NIL), legacy_ten)  # claimed
        legacy_ten = link(int_(5), legacy_ten)  # payment_interval_blocks
        legacy_ten = link(int_(100), legacy_ten)  # payment_amount
        legacy_ten = link(int_(15), legacy_ten)  # next_payment_block_number
        legacy_ten = link(int_(int(LoanType.UNSECURED)), legacy_ten)
        legacy_ten = link(int_(10), legacy_ten)  # payment_count
        legacy_ten = link(int_(900), legacy_ten)  # discounted_amount
        legacy_ten = link(int_(10), legacy_ten)  # creation_block_number
        node.hot_storage[legacy_ten.hash()] = legacy_ten

        loaded = TreasuryLoanRecord.from_storage(node, legacy_ten.hash())
        self.assertIsNone(loaded)


class TestBorrowRequest(unittest.TestCase):
    def test_borrow_request_round_trip_is_two_bytes(self):
        request = TreasuryBorrowRequest(loan_type=LoanType.UNSECURED)

        payload = encode_borrow_request(request)

        self.assertEqual(payload, bytes([BORROW_REQUEST_VERSION, int(LoanType.UNSECURED)]))
        self.assertEqual(len(payload), 2)
        self.assertEqual(decode_borrow_request(payload), request)

    def test_decode_borrow_request_rejects_unknown_loan_type(self):
        self.assertIsNone(decode_borrow_request(bytes([BORROW_REQUEST_VERSION, 99])))

    def test_encode_borrow_request_rejects_unknown_loan_type(self):
        request = TreasuryBorrowRequest(loan_type=99)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            encode_borrow_request(request)


if __name__ == "__main__":
    unittest.main()
