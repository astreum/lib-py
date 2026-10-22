"""Validation tests for the TREASURY_CLOSE (0x23) transaction code.

A close settles a loan early: missed payments are caught up at face value,
and the remaining principal is settled proportionally at the loan's
discounted rate (``discounted_amount * remaining / total_count``).
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

from astreum.consensus.account import create_account
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


class TestTreasuryClose(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_stake=1_000_000, cumulative_fee=1000,
        )
        self.current_height = 5
        self.block = make_block(
            self.node, self.prev_block, height=self.current_height,
        )
        seed_storage_account(self.block)

    def _seed_active_loan(self, sender_pk, *, payment_amount=100, interval=10,
                          next_payment=10, payment_count=5, creation=0,
                          discounted_amount=450,
                          user_balance=10_000, treasury_balance=100_000):
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=LoanType.SECURED,
            discounted_amount=discounted_amount,
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

    def test_close_success(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        discounted = 450
        payment_amount = 100
        loan_tx_id, loan = self._seed_active_loan(
            sender_pk, payment_amount=payment_amount, discounted_amount=discounted,
        )
        total_cost = discounted

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=total_cost, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        self.assertEqual(
            sender.balance,
            sender_before - total_cost - receipt.transaction_fee - receipt.storage_fee,
        )
        self.assertEqual(treasury.balance, treasury_before + total_cost)

        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head.hash())
        self.assertIsNotNone(user)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        updated_loan = TreasuryLoanRecord.from_storage(self.node, loan_head.hash())
        self.assertIsNotNone(updated_loan)
        self.assertEqual(updated_loan.next_payment_block_number, 0)

    def test_close_partially_paid(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        discounted = 450
        payment_amount = 100
        loan_tx_id, loan = self._seed_active_loan(
            sender_pk, payment_amount=payment_amount, discounted_amount=discounted,
            next_payment=30, payment_count=5,
        )
        total_cost = discounted * 3 // 5

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=total_cost, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        self.assertEqual(
            sender.balance,
            sender_before - total_cost - receipt.transaction_fee - receipt.storage_fee,
        )
        self.assertEqual(treasury.balance, treasury_before + total_cost)

        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head.hash())
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        updated_loan = TreasuryLoanRecord.from_storage(self.node, loan_head.hash())
        self.assertIsNotNone(updated_loan)
        self.assertEqual(updated_loan.next_payment_block_number, 0)

        
    # --- past-due catchup ---

    def test_close_past_due_catches_up(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        discounted = 450
        payment_amount = 100
        block = make_block(self.node, self.prev_block, height=25)
        seed_storage_account(block)

        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=0,
            loan_type=LoanType.SECURED,
            discounted_amount=discounted,
            payment_amount=payment_amount,
            payment_interval_blocks=10,
            next_payment_block_number=10,
            payment_count=5,
        )
        block.accounts.set_account(sender_pk, create_account(balance=10_000_000))
        treasury = seed_treasury_account(
            self.node, block, treasury_balance=100_000,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=10_000,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        sender_before = block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        catchup = 2 * payment_amount
        remaining_principal = discounted * 3 // 5
        total_cost = catchup + remaining_principal

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=total_cost, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)

        receipt = block.receipts[-1]
        sender = block.accounts.get_account(sender_pk, self.node)
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        self.assertEqual(
            sender.balance,
            sender_before - total_cost - receipt.transaction_fee - receipt.storage_fee,
        )
        self.assertEqual(treasury.balance, treasury_before + total_cost)

        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head.hash())
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        updated_loan = TreasuryLoanRecord.from_storage(self.node, loan_head.hash())
        self.assertIsNotNone(updated_loan)
        self.assertEqual(updated_loan.next_payment_block_number, 0)

        
    # --- failures ---

    def test_close_insufficient_amount_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        discounted = 450
        loan_tx_id, _ = self._seed_active_loan(
            sender_pk, discounted_amount=discounted,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=discounted - 1, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_close_no_user_record_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=100_000)
        loan_tx_id = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=500, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_close_already_closed_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(
            sender_pk, next_payment=0, payment_count=5,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=500, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_close_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(sender_pk)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=500, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_close_amount_zero_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        loan_tx_id, _ = self._seed_active_loan(sender_pk)
        with self.assertRaises(ValueError):
            create_transaction(
                chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=0, code=TransactionCode.TREASURY_CLOSE,
                loan_transaction_id=loan_tx_id, secret_key=sender_key,
            )


if __name__ == "__main__":
    unittest.main()
