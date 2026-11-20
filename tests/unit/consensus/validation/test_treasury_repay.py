"""Validation tests for the TREASURY_REPAY (0x22) transaction code.

A repay applies one or more whole per-block payments against an open
perpetual loan. The tx data is the 32-byte loan transaction id, and
``tx.amount`` must be a positive multiple of the loan's ``payment_amount``.

Overdue convention: ``paid_until_block`` is the last block whose payment is
settled, so a loan has ``max(0, current_height - paid_until_block)`` blocks
of arrears. ``TREASURY_REPAY`` writes those arrears off for an unsecured
loan, then advances the pointer by ``tx.amount // payment_amount``.
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
from astreum.storage.radix import (
    RadixTree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
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
    store_expr_tree,
    store_tx,
)

PAYMENT = 100
PRINCIPAL = 450


class TestTreasuryRepay(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=1_000_000, cumulative_fee=1000,
            total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, self.prev_block, height=20)
        seed_storage_account(self.block)

    def _make_block(self, height):
        block = make_block(self.node, self.prev_block, height=height)
        seed_storage_account(block)
        return block

    def _seed_loan(
        self,
        block,
        sender_pk,
        *,
        paid_until,
        creation=0,
        loan_type=LoanType.SECURED,
        payment_amount=PAYMENT,
        principal=PRINCIPAL,
        claimed_guarantees=None,
        user_balance=10_000,
        treasury_balance=100_000,
    ):
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment_amount,
            paid_until_block=paid_until,
            claimed_guarantees=claimed_guarantees or [],
        )
        treasury = seed_treasury_account(
            self.node, block, treasury_balance=treasury_balance,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk, user_balance=user_balance,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        return treasury, loan_tx_id, loan

    def _seed_guarantor(self, treasury, guarantor, *, guaranteed):
        record = TreasuryUserRecord(guaranteed=guaranteed)
        head = store_expr_tree(self.node, record.expr())
        put_in_radix_tree(treasury.data, self.node, guarantor, head)
        for trie_node in treasury.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        treasury.data_hash = treasury.data.root_hash or ZERO32

    def _get_user_and_loan(self, block, sender_pk, loan_tx_id):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        loan = TreasuryLoanRecord.from_storage(self.node, loan_head)
        return user, loan

    def _get_guarantor(self, block, guarantor):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        head = get_from_radix_tree(treasury.data, self.node, guarantor)
        return TreasuryUserRecord.from_storage(self.node, head)

    def _repay(self, block, sender_pk, sender_key, loan_tx_id, amount, *, counter=0):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block.receipts[-1]

    # --- success: pointer advance and crediting ---

    def test_repay_advances_pointer_one_block_and_credits_treasury(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)
        sender_before = block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 6)  # current height + one prepaid block
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(user.total_interest_paid, PAYMENT)
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.balance, treasury_before + PAYMENT)
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(sender.balance, sender_before - PAYMENT - receipt.total_fee)

    def test_repay_accepts_a_whole_multiple_and_prepays_past_height(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, 3 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 8)  # 5 -> 6, 7, 8
        self.assertEqual(loan.missed_count, 0)

    def test_repay_credits_a_non_treasury_owner(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=1_000))
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=0,
            loan_type=LoanType.SECURED,
            discounted_amount=PRINCIPAL,
            payment_amount=PAYMENT,
            paid_until_block=5,
            owner=owner,
        )
        treasury = seed_treasury_account(self.node, block, treasury_balance=100_000)
        seed_user_with_loan(
            self.node, treasury, sender=sender_pk, user_balance=10_000,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        treasury_before = treasury.balance

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance, 1_000 + PAYMENT,
        )
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before,
        )

    # --- unsecured write-off ---

    def test_repay_writes_off_overdue_blocks_then_prepays(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
        )
        treasury_before = treasury.balance

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, 2 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 3)  # blocks 6, 7, 8
        self.assertEqual(loan.paid_until_block, 10)  # 8 + two paid blocks
        self.assertEqual(user.defaulted, 3 * PAYMENT)
        self.assertEqual(getattr(block, "global_defaulted", 0), 3 * PAYMENT)
        self.assertEqual(user.total_interest_paid, 2 * PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + 2 * PAYMENT,
        )

    def test_repay_many_blocks_overdue_is_charged_and_does_not_close(self):
        # The perpetual loan has no final due block, so a repay never closes it
        # and never refunds. Every overdue block is written off and the payment
        # advances the pointer past the current height.
        block = self._make_block(height=100)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=1, loan_type=LoanType.UNSECURED,
        )
        treasury_before = treasury.balance
        sender_before = block.accounts.get_account(sender_pk, self.node).balance

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 99)  # blocks 2..100
        self.assertEqual(loan.paid_until_block, 101)  # 100 + one paid block
        self.assertEqual(user.defaulted, 99 * PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PAYMENT,
        )
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance, sender_before - PAYMENT - receipt.total_fee,
        )

    def test_repay_keeps_guarantees_while_the_loan_is_open(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantor = os.urandom(32)
        guarantee_id = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[(guarantor, guarantee_id, 1000)],
        )
        self._seed_guarantor(treasury, guarantor, guaranteed=1000)

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertGreater(loan.paid_until_block, 0)
        self.assertEqual(
            self._get_guarantor(block, guarantor).guaranteed, 1000,
        )

    def test_secured_arrears_are_not_written_off(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=7, loan_type=LoanType.SECURED,
        )

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(getattr(block, "global_defaulted", 0), 0)
        self.assertEqual(loan.paid_until_block, 8)  # one block paid, past the arrears

    # --- overdue boundary: pointer == current_height and both sides ---

    def test_no_write_off_at_exactly_the_current_block(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=8, loan_type=LoanType.UNSECURED,
        )
        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(loan.paid_until_block, 9)

    def test_one_block_is_written_off_before_the_current_block(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=7, loan_type=LoanType.UNSECURED,
        )
        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 1)
        self.assertEqual(user.defaulted, PAYMENT)
        self.assertEqual(loan.paid_until_block, 9)  # 8 + one paid block

    def test_a_prepaid_pointer_writes_off_nothing(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=9, loan_type=LoanType.UNSECURED,
        )
        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(loan.paid_until_block, 10)

    # --- failures ---

    def test_repay_pointer_before_creation_fails(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=4, creation=5,
        )

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_recipient_not_treasury_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=10)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        self.assertEqual(block.receipts[-1].status, STATUS_FAILED)

    def test_amount_zero_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=10)
        with self.assertRaises(ValueError):
            create_transaction(
                chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=0, code=TransactionCode.TREASURY_REPAY,
                loan_transaction_id=loan_tx_id, secret_key=sender_key,
            )

    def test_no_user_record_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        # treasury with no user record for sender
        seed_treasury_account(self.node, block, treasury_balance=100_000)
        loan_tx_id = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, block, tx_hash)
        self.assertEqual(block.receipts[-1].status, STATUS_FAILED)

    def test_amount_not_multiple_of_payment_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=10)

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, 150)

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_below_one_payment_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=10)

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, PAYMENT - 1)

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_fully_paid_loan_fails(self):
        block = self.block
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        # paid_until_block == 0 means closed
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=0)

        receipt = self._repay(block, sender_pk, sender_key, loan_tx_id, 100)

        self.assertEqual(receipt.status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
