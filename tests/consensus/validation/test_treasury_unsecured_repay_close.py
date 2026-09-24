"""Validation tests for unsecured-loan effects on TREASURY_REPAY (0x22) and
TREASURY_CLOSE (0x23): late-installment write-off, permanent default
accrual, and seller `sold_limit` return at loan-end.

Loans are seeded directly (bypassing TREASURY_BORROW) since these tests
only care about repay/close behavior against an already-originated
unsecured loan.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, get_radix_node_expr, put_in_radix_tree
from astreum.storage.radix.node import radix_node_hash
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_expr_tree,
    store_tx,
)

PAYMENT_AMOUNT = 100
INTERVAL = 10
PAYMENT_COUNT = 5
DISCOUNTED = 450  # < payment_amount * payment_count (500), so there's interest
CREATION = 0
FINAL = CREATION + INTERVAL * PAYMENT_COUNT  # 50


class _UnsecuredRepayCloseTestBase(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(cumulative_stake=1_000_000, cumulative_fee=1000)

    def _make_block(self, height):
        block = make_block(self.node, self.prev_block, height=height)
        seed_storage_account(block)
        return block

    def _seed_loan(
        self,
        block,
        sender_pk,
        *,
        next_payment,
        claimed_offers=None,
        payment_count=PAYMENT_COUNT,
        discounted_amount=DISCOUNTED,
        treasury_balance=100_000,
        user_balance=0,
    ):
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=CREATION,
            loan_type=LoanType.UNSECURED,
            discounted_amount=discounted_amount,
            payment_amount=PAYMENT_AMOUNT,
            payment_interval_blocks=INTERVAL,
            next_payment_block_number=next_payment,
            payment_count=payment_count,
            claimed_offers=claimed_offers or [],
            insurance_fee=0,
            missed_count=0,
        )
        treasury = seed_treasury_account(self.node, block, treasury_balance=treasury_balance)
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=user_balance,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        return treasury, loan_tx_id, loan

    def _seed_seller(self, treasury, seller, *, sold_limit, total_interest_paid=0):
        record = TreasuryUserRecord(sold_limit=sold_limit, total_interest_paid=total_interest_paid)
        head = store_expr_tree(self.node, record.expr())
        put_in_radix_tree(treasury.data, self.node, seller, head)
        for trie_node in treasury.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        treasury.data_hash = treasury.data.root_hash or ZERO32

    def _get_loan(self, block, sender_pk, loan_tx_id):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        return user, TreasuryLoanRecord.from_storage(self.node, loan_head)

    def _get_seller(self, block, seller):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        head = get_from_radix_tree(treasury.data, self.node, seller)
        return TreasuryUserRecord.from_storage(self.node, head)


class TestTreasuryUnsecuredRepay(_UnsecuredRepayCloseTestBase):
    def test_late_installments_written_off_then_next_charged(self):
        block = self._make_block(height=25)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        treasury, loan_tx_id, loan = self._seed_loan(block, sender_pk, next_payment=10)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        user, updated_loan = self._get_loan(block, sender_pk, loan_tx_id)
        # Installments due at 10 and 20 were overdue (height=25) -> written off.
        self.assertEqual(updated_loan.missed_count, 2)
        # This tx's single payment lands on the block-30 installment.
        self.assertEqual(updated_loan.next_payment_block_number, 40)
        self.assertEqual(user.defaulted, 2 * PAYMENT_AMOUNT)
        self.assertEqual(block.global_defaulted, 2 * PAYMENT_AMOUNT)
        # Interest: total_interest = 500-450=50; before=50*2//5=20; after=50*3//5=30.
        self.assertEqual(user.total_interest_paid, 10)

    def test_loan_past_final_due_block_closes_as_defaulted_and_refunds(self):
        block = self._make_block(height=100)  # well past FINAL (50)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        treasury, loan_tx_id, loan = self._seed_loan(block, sender_pk, next_payment=10)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        receipt = block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, updated_loan = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(updated_loan.next_payment_block_number, 0)
        self.assertEqual(updated_loan.missed_count, PAYMENT_COUNT)
        self.assertEqual(user.defaulted, PAYMENT_COUNT * PAYMENT_AMOUNT)
        self.assertEqual(block.global_defaulted, PAYMENT_COUNT * PAYMENT_AMOUNT)

        sender = block.accounts.get_account(sender_pk, self.node)
        # Amount was refunded: net change is only the ordinary tx/storage fees.
        self.assertEqual(
            sender.balance,
            sender_before - receipt.transaction_fee - receipt.storage_fee,
        )

    def test_two_sequential_repays_do_not_double_count(self):
        block = self._make_block(height=25)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        treasury, loan_tx_id, loan = self._seed_loan(block, sender_pk, next_payment=10)

        tx1 = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx1_hash = store_tx(self.node, tx1)
        apply_transaction(self.node, block, tx1_hash)
        flush_pending(self.node, block)

        user_after_1, loan_after_1 = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan_after_1.missed_count, 2)
        self.assertEqual(user_after_1.defaulted, 200)

        # Second repay at the same height: next_payment_block_number is now
        # 40 (> height 25), so nothing further is written off.
        tx2 = create_transaction(
            chain_id=1, counter=1, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx2_hash = store_tx(self.node, tx2)
        apply_transaction(self.node, block, tx2_hash)
        flush_pending(self.node, block)

        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        user_after_2, loan_after_2 = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan_after_2.missed_count, 2)  # unchanged
        self.assertEqual(user_after_2.defaulted, 200)  # unchanged
        self.assertEqual(block.global_defaulted, 200)  # unchanged
        self.assertEqual(loan_after_2.next_payment_block_number, 50)

    def test_clean_full_payoff_returns_full_seller_limit(self):
        block = self._make_block(height=5)  # before any installment is due
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        seller_pk = os.urandom(32)
        offer_id = os.urandom(32)
        limit = 1000
        treasury, loan_tx_id, loan = self._seed_loan(
            block, sender_pk, next_payment=10,
            claimed_offers=[(seller_pk, offer_id, limit)],
        )
        self._seed_seller(treasury, seller_pk, sold_limit=limit)

        # Pay off every installment one at a time, all within this same
        # block (height stays fixed at 5, well before every due date, so
        # nothing is ever written off).
        for counter in range(PAYMENT_COUNT):
            tx = create_transaction(
                chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_REPAY,
                loan_transaction_id=loan_tx_id, secret_key=sender_key,
            )
            tx_hash = store_tx(self.node, tx)
            apply_transaction(self.node, block, tx_hash)
            flush_pending(self.node, block)
            self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)

        user, final_loan = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(final_loan.next_payment_block_number, 0)
        self.assertEqual(final_loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)

        seller_record = self._get_seller(block, seller_pk)
        self.assertEqual(seller_record.sold_limit, 0)  # full limit returned


class TestTreasuryUnsecuredClose(_UnsecuredRepayCloseTestBase):
    def test_total_default_returns_nothing_to_seller(self):
        block = self._make_block(height=100)  # past FINAL for every installment
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        seller_pk = os.urandom(32)
        offer_id = os.urandom(32)
        limit = 1000
        treasury, loan_tx_id, loan = self._seed_loan(
            block, sender_pk, next_payment=10,
            claimed_offers=[(seller_pk, offer_id, limit)],
        )
        self._seed_seller(treasury, seller_pk, sold_limit=limit)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=PAYMENT_AMOUNT, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        receipt = block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, updated_loan = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(updated_loan.next_payment_block_number, 0)
        self.assertEqual(updated_loan.missed_count, PAYMENT_COUNT)
        self.assertEqual(user.defaulted, PAYMENT_COUNT * PAYMENT_AMOUNT)

        seller_record = self._get_seller(block, seller_pk)
        self.assertEqual(seller_record.sold_limit, limit)  # nothing returned

        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance,
            sender_before - receipt.transaction_fee - receipt.storage_fee,
        )

    def test_partial_default_returns_proportional_seller_limit(self):
        block = self._make_block(height=25)  # installments at 10, 20 overdue
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        seller_pk = os.urandom(32)
        offer_id = os.urandom(32)
        limit = 1000
        treasury, loan_tx_id, loan = self._seed_loan(
            block, sender_pk, next_payment=10,
            claimed_offers=[(seller_pk, offer_id, limit)],
        )
        self._seed_seller(treasury, seller_pk, sold_limit=limit)

        # remaining_count = 5 - 2 = 3; remaining_principal = 450*3//5 = 270.
        total_cost = DISCOUNTED * 3 // 5
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=total_cost, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        user, updated_loan = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(updated_loan.next_payment_block_number, 0)
        self.assertEqual(updated_loan.missed_count, 2)
        self.assertEqual(user.defaulted, 2 * PAYMENT_AMOUNT)

        seller_record = self._get_seller(block, seller_pk)
        # paid_count = 5 - 2 = 3; returned = 1000 * 3 // 5 = 600.
        self.assertEqual(seller_record.sold_limit, limit - 600)

    def test_secured_close_unaffected_by_unsecured_changes(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000_000)
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=CREATION,
            loan_type=LoanType.SECURED,
            discounted_amount=DISCOUNTED,
            payment_amount=PAYMENT_AMOUNT,
            payment_interval_blocks=INTERVAL,
            next_payment_block_number=10,
            payment_count=PAYMENT_COUNT,
        )
        treasury = seed_treasury_account(self.node, block, treasury_balance=100_000)
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=10_000,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        total_cost = DISCOUNTED

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=total_cost, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        user, updated_loan = self._get_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(updated_loan.next_payment_block_number, 0)
        self.assertEqual(updated_loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(block.global_defaulted, 0)


if __name__ == "__main__":
    unittest.main()
