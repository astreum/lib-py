"""Unit tests for the stake-return repayment path in ``treasury/loans.py``.

The validator's stake-return path pays whole per-block payments into a
borrower's open secured loans. It moves ``paid_until_block`` by one block per
payment, the same semantics as ``TREASURY_REPAY``, and leaves any amount that
is smaller than one payment staked.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.transaction.treasury.loans import (
    apply_treasury_loan_payments_from_stake_return,
    load_user_loan,
    write_user_loan,
)
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    load_user_record,
)
from astreum.expression import ZERO32
from astreum.storage.radix import (
    RadixTree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_storage_account,
    seed_treasury_account,
    store_expr_tree,
)

PAYMENT = 100


class TestStakeReturnRepayment(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=1_000_000, cumulative_fee=1000,
            )
        self.block = make_block(self.node, self.prev_block, height=5)
        seed_storage_account(self.block)
        self.borrower = os.urandom(32)

    def _seed_user_loans(self, loans, *, user_balance, treasury_balance=100_000):
        loans_trie = RadixTree()
        for loan_tx_id, loan in loans.items():
            loan_head = store_expr_tree(self.node, loan.expr())
            put_in_radix_tree(loans_trie, self.node, loan_tx_id, loan_head)
        for trie_node in loans_trie.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)

        record = TreasuryUserRecord(
            balance=user_balance,
            loans_root_hash=loans_trie.root_hash or ZERO32,
        )
        return seed_treasury_account(
            self.node, self.block, treasury_balance=treasury_balance,
            user_records={self.borrower: record},
        )

    def _loan(
        self,
        paid_until,
        *,
        loan_type=LoanType.SECURED,
        creation=0,
        payment_amount=PAYMENT,
        principal=450,
    ):
        return TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment_amount,
            paid_until_block=paid_until,
        )

    def _load_user(self):
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        return load_user_record(self.node, treasury.data, self.borrower)

    def _load_loan(self, loan_tx_id):
        user = self._load_user()
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        return TreasuryLoanRecord.from_storage(self.node, loan_head)

    def _apply(self, amount):
        applied = apply_treasury_loan_payments_from_stake_return(
            node=self.node,
            accounts=self.block.accounts,
            borrower=self.borrower,
            amount=amount,
        )
        # The stake-return path runs outside a transaction apply, so its exprs
        # land on the accounts cache rather than the block.
        for expr in self.block.accounts.pending_exprs:
            self.node.hot_storage[expr.hash()] = expr
        flush_pending(self.node, self.block)
        return applied

    def test_one_payment_advances_the_pointer_one_block(self):
        loan_tx_id = os.urandom(32)
        self._seed_user_loans({loan_tx_id: self._loan(5)}, user_balance=10_000)

        self.assertTrue(self._apply(PAYMENT))

        user = self._load_user()
        loan = self._load_loan(loan_tx_id)
        self.assertEqual(loan.paid_until_block, 6)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.balance, 10_000 - PAYMENT)
        self.assertEqual(user.total_interest_paid, PAYMENT)
        self.assertEqual(
            self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            100_000 + PAYMENT,
        )

    def test_remainder_smaller_than_one_payment_stays_staked(self):
        loan_tx_id = os.urandom(32)
        self._seed_user_loans({loan_tx_id: self._loan(5)}, user_balance=10_000)

        self.assertTrue(self._apply(2 * PAYMENT + 50))

        user = self._load_user()
        loan = self._load_loan(loan_tx_id)
        self.assertEqual(loan.paid_until_block, 7)  # two payments of one block
        self.assertEqual(user.balance, 10_000 - 2 * PAYMENT)
        self.assertEqual(
            self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            100_000 + 2 * PAYMENT,
        )

    def test_payments_are_spread_across_open_secured_loans(self):
        first = os.urandom(32)
        second = os.urandom(32)
        self._seed_user_loans(
            {first: self._loan(5), second: self._loan(7)},
            user_balance=10_000,
        )

        self.assertTrue(self._apply(3 * PAYMENT))

        self.assertEqual(self._load_loan(first).paid_until_block, 7)
        self.assertEqual(self._load_loan(second).paid_until_block, 8)
        self.assertEqual(self._load_user().balance, 10_000 - 3 * PAYMENT)

    def test_unsecured_and_closed_loans_are_skipped(self):
        secured = os.urandom(32)
        unsecured = os.urandom(32)
        closed = os.urandom(32)
        self._seed_user_loans(
            {
                secured: self._loan(5),
                unsecured: self._loan(5, loan_type=LoanType.UNSECURED),
                closed: self._loan(0),
            },
            user_balance=10_000,
        )

        self.assertTrue(self._apply(PAYMENT))

        self.assertEqual(self._load_loan(secured).paid_until_block, 6)
        self.assertEqual(self._load_loan(unsecured).paid_until_block, 5)
        self.assertEqual(self._load_loan(closed).paid_until_block, 0)
        self.assertEqual(self._load_user().balance, 10_000 - PAYMENT)

    def test_insufficient_user_balance_returns_false_without_changes(self):
        loan_tx_id = os.urandom(32)
        self._seed_user_loans({loan_tx_id: self._loan(5)}, user_balance=150)

        self.assertFalse(self._apply(200))

        self.assertEqual(self._load_loan(loan_tx_id).paid_until_block, 5)
        self.assertEqual(self._load_user().balance, 150)

    def test_user_without_loans_returns_true_unchanged(self):
        seed_treasury_account(
            self.node, self.block, treasury_balance=100_000,
            user_records={self.borrower: TreasuryUserRecord(balance=500)},
        )

        self.assertTrue(self._apply(PAYMENT))

        self.assertEqual(self._load_user().balance, 500)
        self.assertEqual(
            self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            100_000,
        )


_UNSET = object()


class TestLoanTrieReadWrite(unittest.TestCase):
    """The loans trie read/write path shared by every loan type."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=1_000_000, cumulative_fee=1000,
            )
        self.block = make_block(self.node, self.prev_block, height=5)
        seed_storage_account(self.block)
        self.borrower = os.urandom(32)
        seed_treasury_account(self.node, self.block, treasury_balance=100_000)

    def _treasury(self):
        return self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

    def _load_user(self):
        return load_user_record(self.node, self._treasury().data, self.borrower)

    def _seed_record(self, *, balance=10_000, loans=None, **fields):
        loans_trie = RadixTree()
        for loan_tx_id, loan in (loans or {}).items():
            loan_head = store_expr_tree(self.node, loan.expr())
            put_in_radix_tree(loans_trie, self.node, loan_tx_id, loan_head)
        for trie_node in loans_trie.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        record = TreasuryUserRecord(
            balance=balance,
            loans_root_hash=loans_trie.root_hash or ZERO32,
            **fields,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=100_000,
            user_records={self.borrower: record},
        )
        return record

    def _write(self, loan, loan_tx_id, user_record=_UNSET):
        if user_record is _UNSET:
            user_record = self._load_user()
        updated = write_user_loan(
            node=self.node,
            treasury_account=self._treasury(),
            borrower=self.borrower,
            user_record=user_record,
            loan_transaction_id=loan_tx_id,
            loan=loan,
            pending_exprs=self.block.accounts.pending_exprs,
            contact=self.block.contact,
        )
        for expr in self.block.accounts.pending_exprs:
            self.node.hot_storage[expr.hash()] = expr
        return updated

    def _load_loan(self, loan_tx_id):
        return load_user_loan(
            node=self.node,
            treasury_account=self._treasury(),
            borrower=self.borrower,
            loan_transaction_id=loan_tx_id,
            contact=self.block.contact,
        )

    def _loan(self, *, loan_type=LoanType.CLAIM_COST, principal=1_200, payment=6, creation=5):
        return TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment,
            paid_until_block=creation,
        )

    def test_load_without_a_user_record_returns_none(self):
        self.assertIsNone(self._load_user())
        self.assertIsNone(self._load_loan(os.urandom(32)))

    def test_write_then_read_claim_cost_on_an_empty_record(self):
        loan_tx_id = os.urandom(32)
        loan = self._loan()

        updated = self._write(loan, loan_tx_id)

        self.assertIsNotNone(updated)
        self.assertNotEqual(updated.loans_root_hash, ZERO32)
        user = self._load_user()
        self.assertIsNotNone(user)
        self.assertEqual(user.loans_root_hash, updated.loans_root_hash)
        loaded = self._load_loan(loan_tx_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.loan_type, LoanType.CLAIM_COST)
        self.assertEqual(loaded.creation_block_number, 5)
        self.assertEqual(loaded.discounted_amount, 1_200)
        self.assertEqual(loaded.payment_amount, 6)
        self.assertEqual(loaded.paid_until_block, 5)
        self.assertEqual(loaded.owner, TREASURY_ADDRESS)

    def test_write_then_read_claim_cost_beside_an_existing_loan(self):
        secured_tx_id = os.urandom(32)
        secured = self._loan(loan_type=LoanType.SECURED, principal=500, payment=100)
        self._seed_record(loans={secured_tx_id: secured})

        claim_tx_id = os.urandom(32)
        claim = self._loan(principal=2_400, payment=12)
        user = self._load_user()
        self.assertIsNotNone(user)
        self.assertIsNotNone(self._write(claim, claim_tx_id, user_record=user))

        loaded_user = self._load_user()
        self.assertIsNotNone(loaded_user)
        self.assertEqual(loaded_user.balance, 10_000)
        loaded_secured = self._load_loan(secured_tx_id)
        loaded_claim = self._load_loan(claim_tx_id)
        self.assertIsNotNone(loaded_secured)
        self.assertIsNotNone(loaded_claim)
        self.assertEqual(loaded_secured.loan_type, LoanType.SECURED)
        self.assertEqual(loaded_secured.paid_until_block, 5)
        self.assertEqual(loaded_claim.loan_type, LoanType.CLAIM_COST)
        self.assertEqual(loaded_claim.discounted_amount, 2_400)
        self.assertEqual(loaded_claim.payment_amount, 12)

    def test_none_user_record_loads_the_existing_record(self):
        existing_tx_id = os.urandom(32)
        existing = self._loan(loan_type=LoanType.SECURED, principal=500, payment=100)
        self._seed_record(
            balance=10_000,
            loans={existing_tx_id: existing},
            total_interest_paid=77,
            loaned=33,
            defaulted=4,
        )

        claim_tx_id = os.urandom(32)
        updated = self._write(
            self._loan(principal=2_400, payment=12), claim_tx_id, user_record=None
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated.balance, 10_000)
        self.assertEqual(updated.total_interest_paid, 77)
        self.assertEqual(updated.loaned, 33)
        self.assertEqual(updated.defaulted, 4)
        user = self._load_user()
        self.assertIsNotNone(user)
        self.assertEqual(user.balance, 10_000)
        self.assertEqual(user.total_interest_paid, 77)
        self.assertEqual(user.loaned, 33)
        self.assertEqual(user.defaulted, 4)
        self.assertIsNotNone(self._load_loan(existing_tx_id))
        self.assertIsNotNone(self._load_loan(claim_tx_id))

    def test_write_with_an_empty_loans_root_succeeds(self):
        self._seed_record(balance=10_000)
        user = self._load_user()
        self.assertIsNotNone(user)
        self.assertEqual(user.loans_root_hash, ZERO32)

        loan_tx_id = os.urandom(32)
        updated = self._write(self._loan(), loan_tx_id, user_record=user)

        self.assertIsNotNone(updated)
        self.assertNotEqual(updated.loans_root_hash, ZERO32)
        loaded_user = self._load_user()
        self.assertIsNotNone(loaded_user)
        self.assertEqual(loaded_user.balance, 10_000)
        self.assertIsNotNone(self._load_loan(loan_tx_id))

    def test_duplicate_loan_key_is_rejected(self):
        loan_tx_id = os.urandom(32)
        self.assertIsNotNone(self._write(self._loan(principal=1_200), loan_tx_id))
        root_before = self._load_user().loans_root_hash

        duplicate = self._write(self._loan(principal=9_999), loan_tx_id)

        self.assertIsNone(duplicate)
        self.assertEqual(self._load_user().loans_root_hash, root_before)
        loaded = self._load_loan(loan_tx_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.discounted_amount, 1_200)

    def test_existing_loan_types_round_trip_through_the_trie(self):
        user = None
        for loan_type in (LoanType.SECURED, LoanType.UNSECURED):
            loan_tx_id = os.urandom(32)
            user = self._write(self._loan(loan_type=loan_type), loan_tx_id, user_record=user)
            self.assertIsNotNone(user)
            loaded = self._load_loan(loan_tx_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.loan_type, loan_type)


if __name__ == "__main__":
    unittest.main()
