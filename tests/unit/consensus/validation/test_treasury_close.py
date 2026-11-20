"""Validation tests for the TREASURY_CLOSE (0x23) transaction code.

A close ends an open perpetual loan. A secured loan pays the full principal
plus the secured catch-up for its arrears, at face value. An unsecured loan
pays the full principal, and its arrears are written off to ``defaulted``.

Overdue convention: ``paid_until_block`` is the last block whose payment is
settled, so ``overdue_blocks = max(0, current_height - paid_until_block)``.
A close sets ``paid_until_block = 0``.
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


class TestTreasuryClose(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=1_000_000, cumulative_fee=1000,
            total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, self.prev_block, height=5)
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
        owner=TREASURY_ADDRESS,
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
            owner=owner,
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

    def _close(self, block, sender_pk, sender_key, loan_tx_id, amount, *, counter=0):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block.receipts[-1]

    # --- secured success ---

    def test_close_secured_pays_principal_without_catchup(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)
        sender_before = block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 0)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.total_interest_paid, 0)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance, sender_before - PRINCIPAL - receipt.total_fee,
        )

    def test_close_secured_charges_the_catchup(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)
        treasury_before = treasury.balance
        total_cost = PRINCIPAL + 3 * PAYMENT

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, total_cost)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 0)
        self.assertEqual(user.total_interest_paid, 3 * PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + total_cost,
        )

    def test_close_secured_refunds_excess_to_the_sender_account(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)
        sender_before = block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = treasury.balance
        total_cost = PRINCIPAL + 3 * PAYMENT

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, 800)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance, sender_before - total_cost - receipt.total_fee,
        )
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + total_cost,
        )
        user, _ = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(user.balance, 10_000)  # refund is not staked

    def test_close_secured_splits_principal_and_catchup_between_owners(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=7))
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, owner=owner,
        )
        treasury_before = treasury.balance
        total_cost = PRINCIPAL + 3 * PAYMENT

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, total_cost)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )
        self.assertEqual(
            block.accounts.get_account(owner, self.node).balance,
            7 + 3 * PAYMENT,
        )

    # --- secured overdue boundary ---

    def test_close_secured_at_exactly_the_current_block_has_no_catchup(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=8)
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, _ = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(user.total_interest_paid, 0)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    def test_close_secured_one_block_before_the_current_block_has_one_catchup(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=7)
        treasury_before = treasury.balance

        receipt = self._close(
            block, sender_pk, sender_key, loan_tx_id, PRINCIPAL + PAYMENT,
        )

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, _ = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(user.total_interest_paid, PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL + PAYMENT,
        )

    def test_close_secured_with_a_prepaid_pointer_has_no_catchup(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=9)
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, _ = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(user.total_interest_paid, 0)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    def test_close_secured_insufficient_for_the_catchup_fails(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(block, sender_pk, paid_until=5)

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL + 3 * PAYMENT - 1)

        self.assertEqual(receipt.status, STATUS_FAILED)

    # --- unsecured success ---

    def test_close_unsecured_charges_principal_and_writes_off_arrears(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantor = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[(guarantor, os.urandom(32), 1000)],
        )
        self._seed_guarantor(treasury, guarantor, guaranteed=1000)
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 0)
        self.assertEqual(loan.missed_count, 3)
        self.assertEqual(user.defaulted, 3 * PAYMENT)
        self.assertEqual(getattr(block, "global_defaulted", 0), 3 * PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )
        # 300 of the 1000 guarantee is permanently lost; the rest is released.
        self.assertEqual(
            self._get_guarantor(block, guarantor).guaranteed, 3 * PAYMENT,
        )

    def test_close_unsecured_clean_payoff_releases_the_full_guarantee(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantor = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[(guarantor, os.urandom(32), 1000)],
        )
        self._seed_guarantor(treasury, guarantor, guaranteed=1000)

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(self._get_guarantor(block, guarantor).guaranteed, 0)

    def test_close_unsecured_total_default_releases_nothing(self):
        block = self._make_block(height=25)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantor = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[(guarantor, os.urandom(32), 1000)],
        )
        self._seed_guarantor(treasury, guarantor, guaranteed=1000)

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 20)  # write-off 2000 >= guarantee 1000
        self.assertEqual(user.defaulted, 20 * PAYMENT)
        self.assertEqual(self._get_guarantor(block, guarantor).guaranteed, 1000)

    def test_close_unsecured_splits_the_guarantee_loss_across_guarantors(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        first_guarantor = os.urandom(32)
        second_guarantor = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[
                (first_guarantor, os.urandom(32), 600),
                (second_guarantor, os.urandom(32), 400),
            ],
        )
        self._seed_guarantor(treasury, first_guarantor, guaranteed=600)
        self._seed_guarantor(treasury, second_guarantor, guaranteed=400)

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # 300 written off, split 600:400 -> permanent losses 180:120.
        self.assertEqual(
            self._get_guarantor(block, first_guarantor).guaranteed, 180,
        )
        self.assertEqual(
            self._get_guarantor(block, second_guarantor).guaranteed, 120,
        )

    def test_close_unsecured_guarantee_loss_integer_division_remainder(self):
        block = self._make_block(height=338)  # 333 blocks past pointer 5
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantors = [os.urandom(32) for _ in range(3)]
        amounts = [333, 333, 334]
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            payment_amount=1, principal=450,
            claimed_guarantees=[
                (guarantor, os.urandom(32), amount)
                for guarantor, amount in zip(guarantors, amounts)
            ],
        )
        for guarantor, amount in zip(guarantors, amounts):
            self._seed_guarantor(treasury, guarantor, guaranteed=amount)

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 333)
        self.assertEqual(user.defaulted, 333)
        # Each loss floors: 333*333//1000 = 110, 333*333//1000 = 110,
        # 333*334//1000 = 111. The 2-unit remainder of the write-off is
        # released, so the retained loss is 331, not 333.
        self.assertEqual(
            [self._get_guarantor(block, guarantor).guaranteed for guarantor in guarantors],
            [110, 110, 111],
        )

    def test_close_after_a_repay_write_off_retains_the_earlier_loss(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        guarantor = os.urandom(32)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            claimed_guarantees=[(guarantor, os.urandom(32), 1000)],
        )
        self._seed_guarantor(treasury, guarantor, guaranteed=1000)

        repay_receipt = self._repay(
            block, sender_pk, sender_key, loan_tx_id, PAYMENT,
        )
        self.assertEqual(repay_receipt.status, STATUS_SUCCESS)
        _, after_repay = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(after_repay.missed_count, 3)  # blocks 6, 7, 8
        self.assertEqual(after_repay.paid_until_block, 9)

        # The pointer is now ahead of the chain, so the close is clean. The
        # guarantee release must still charge the repay's earlier write-off.
        close_receipt = self._close(
            block, sender_pk, sender_key, loan_tx_id, PRINCIPAL, counter=1,
        )

        self.assertEqual(close_receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 0)
        self.assertEqual(loan.missed_count, 3)
        self.assertEqual(user.defaulted, 3 * PAYMENT)
        self.assertEqual(getattr(block, "global_defaulted", 0), 3 * PAYMENT)
        self.assertEqual(
            self._get_guarantor(block, guarantor).guaranteed, 3 * PAYMENT,
        )

    def test_close_unsecured_refunds_excess_to_the_sender_account(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
        )
        sender_before = block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, 600)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance, sender_before - PRINCIPAL - receipt.total_fee,
        )
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    def test_close_unsecured_credits_a_non_treasury_owner(self):
        block = self._make_block(height=5)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        owner = os.urandom(32)
        block.accounts.set_account(owner, create_account(balance=3))
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
            owner=owner,
        )
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(block.accounts.get_account(owner, self.node).balance, 3 + PRINCIPAL)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before,
        )

    def test_close_unsecured_insufficient_amount_fails(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=5, loan_type=LoanType.UNSECURED,
        )

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL - 1)

        self.assertEqual(receipt.status, STATUS_FAILED)

    # --- unsecured overdue boundary ---

    def test_close_unsecured_at_exactly_the_current_block_writes_off_nothing(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=8, loan_type=LoanType.UNSECURED,
        )
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(getattr(block, "global_defaulted", 0), 0)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    def test_close_unsecured_one_block_before_the_current_block_writes_off_one(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=7, loan_type=LoanType.UNSECURED,
        )
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 1)
        self.assertEqual(user.defaulted, PAYMENT)
        self.assertEqual(getattr(block, "global_defaulted", 0), PAYMENT)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    def test_close_unsecured_with_a_prepaid_pointer_writes_off_nothing(self):
        block = self._make_block(height=8)
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, sender_pk, paid_until=9, loan_type=LoanType.UNSECURED,
        )
        treasury_before = treasury.balance

        receipt = self._close(block, sender_pk, sender_key, loan_tx_id, PRINCIPAL)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, sender_pk, loan_tx_id)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before + PRINCIPAL,
        )

    # --- failures ---

    def test_close_insufficient_amount_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(self.block, sender_pk, paid_until=10)

        receipt = self._close(
            self.block, sender_pk, sender_key, loan_tx_id, PRINCIPAL - 1,
        )

        self.assertEqual(receipt.status, STATUS_FAILED)

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
        _, loan_tx_id, _ = self._seed_loan(self.block, sender_pk, paid_until=0)
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
        _, loan_tx_id, _ = self._seed_loan(self.block, sender_pk, paid_until=10)
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
        _, loan_tx_id, _ = self._seed_loan(self.block, sender_pk, paid_until=10)
        with self.assertRaises(ValueError):
            create_transaction(
                chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=0, code=TransactionCode.TREASURY_CLOSE,
                loan_transaction_id=loan_tx_id, secret_key=sender_key,
            )


if __name__ == "__main__":
    unittest.main()
