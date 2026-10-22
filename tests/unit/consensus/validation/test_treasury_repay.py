"""Validation tests for the TREASURY_REPAY (0x22) transaction code.

A repay applies one or more scheduled payments against an existing loan. The
tx data is the 32-byte loan transaction id (the tx_hash that created the
loan), and tx.amount must be a positive multiple of the loan's payment_amount.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
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
from astreum.storage.radix import RadixTree, get_from_radix_tree
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)


class TestTreasuryRepay(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_stake=1_000_000, cumulative_fee=1000,
        )
        self.block = make_block(self.node, self.prev_block, height=20)
        seed_storage_account(self.block)

    def _seed_active_loan(self, sender_pk, *, payment_amount=100, interval=10,
                          next_payment=10, payment_count=5, creation=0,
                          user_balance=10_000, treasury_balance=100_000):
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=LoanType.SECURED,
            discounted_amount=450,
            payment_amount=payment_amount,
            payment_interval_blocks=interval,
            next_payment_block_number=next_payment,
            payment_count=payment_count,
        )
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=treasury_balance,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=user_balance,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        return loan_tx_id, loan

    # --- success ---

    def test_repay_advances_loan_and_credits_treasury(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=100_000_000)
        loan_tx_id, loan = self._seed_active_loan(
            sender_pk, payment_amount=100, interval=10,
            next_payment=10, payment_count=5,
        )
        repay_amount = 100  # exactly one payment

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=repay_amount, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        treasury_before = self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(treasury.balance, treasury_before + repay_amount)

        # Loan advanced by one interval.
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head.hash())
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        updated_loan = TreasuryLoanRecord.from_storage(self.node, loan_head)
        self.assertIsNotNone(updated_loan)
        self.assertEqual(updated_loan.next_payment_block_number, 20)  # 10 + 10

    # --- failures ---

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(sender_pk)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_amount_zero_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(sender_pk)
        with self.assertRaises(ValueError):
            create_transaction(
                chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=0, code=TransactionCode.TREASURY_REPAY,
                loan_transaction_id=loan_tx_id, secret_key=sender_key,
            )

    def test_no_user_record_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        # treasury with no user record for sender
        seed_treasury_account(self.node, self.block, treasury_balance=100_000)
        loan_tx_id = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_amount_not_multiple_of_payment_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(sender_pk, payment_amount=100)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=150, code=TransactionCode.TREASURY_REPAY,  # not a multiple of 100
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_fully_paid_loan_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        # next_payment_block_number == 0 means fully paid
        loan_tx_id, _ = self._seed_active_loan(
            sender_pk, next_payment=0, payment_count=5,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
