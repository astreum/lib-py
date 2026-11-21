"""Validation tests for the TREASURY_CLAIM (0x26) transaction code.

A non-treasury owner of a secured loan claims its overdue payments from the
treasury and advances the pointer. Under the ratified convention
``paid_until_block`` is the last settled block, so the claim pays

    max(0, current_height - paid_until_block) * payment_amount

and then sets ``paid_until_block = current_height``. A claim never moves the
pointer backwards: at or ahead of the current block it has nothing to claim
and fails.
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
from astreum.consensus.transaction.treasury.claim import handle_treasury_claim
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.expression import NIL, bytes_, link
from astreum.storage.radix import (
    RadixTree,
    get_from_radix_tree,
)
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

PAYMENT = 100
PRINCIPAL = 450
TREASURY_BALANCE = 100_000


def _claim_data(loan_tx_id):
    """Encode a claim's data: a one-element link-list holding the loan id."""
    return link(bytes_(loan_tx_id), NIL)


class TestTreasuryClaim(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=100_000, cumulative_fee=1000,
            )

    def _make_block(self, height):
        block = make_block(self.node, self.prev_block, height=height)
        seed_storage_account(block)
        return block

    def _seed_loan_tx(self, borrower_pk):
        """Store a real borrow transaction; the loan is keyed by its hash.

        ``TREASURY_CLAIM`` finds the borrower through the loan's originating
        transaction, so the trie key must be a stored transaction hash.
        """
        loan_tx = create_transaction(
            chain_id=1, counter=0, sender=borrower_pk,
            recipient=TREASURY_ADDRESS, amount=PRINCIPAL,
            code=TransactionCode.TREASURY_BORROW,
        )
        return store_tx(self.node, loan_tx)

    def _seed_loan(
        self,
        block,
        borrower_pk,
        *,
        paid_until,
        owner,
        creation=0,
        loan_type=LoanType.SECURED,
        payment_amount=PAYMENT,
        principal=PRINCIPAL,
        user_balance=10_000,
        treasury_balance=TREASURY_BALANCE,
    ):
        loan_tx_id = self._seed_loan_tx(borrower_pk)
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment_amount,
            paid_until_block=paid_until,
            owner=owner,
        )
        treasury = seed_treasury_account(
            self.node, block, treasury_balance=treasury_balance,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=borrower_pk, user_balance=user_balance,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        return treasury, loan_tx_id, loan

    def _get_user_and_loan(self, block, borrower_pk, loan_tx_id):
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, borrower_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, loan_tx_id)
        loan = TreasuryLoanRecord.from_storage(self.node, loan_head)
        return user, loan

    def _claim(self, block, claimant_pk, claimant_key, loan_tx_id, *, counter=0,
               amount=0, recipient=TREASURY_ADDRESS):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=claimant_pk, recipient=recipient,
            amount=amount, code=TransactionCode.TREASURY_CLAIM,
            data=_claim_data(loan_tx_id), secret_key=claimant_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block.receipts[-1]

    def _seed_owner(self, block, *, balance=100_000):
        owner_pk, owner_key = seed_sender_account(block, balance=balance)
        return owner_pk, owner_key

    def _apply_claim_handler_only(self, block, claimant_pk, loan_tx_id):
        """Call the handler directly, without ``apply``'s snapshot rollback."""
        tx = create_transaction(
            chain_id=1, counter=0, sender=claimant_pk, recipient=TREASURY_ADDRESS,
            amount=0, code=TransactionCode.TREASURY_CLAIM,
            data=_claim_data(loan_tx_id),
        )
        return handle_treasury_claim(
            node=self.node, block=block, transaction=tx,
            transaction_hash=os.urandom(32),
        )

    # --- payout and pointer advance ---

    def test_claim_pays_the_overdue_blocks_and_advances_the_pointer(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
        )
        treasury_before = treasury.balance

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 8)
        self.assertEqual(
            block.accounts.get_account(owner_pk, self.node).balance,
            100_000 + 3 * PAYMENT - receipt.total_fee,
        )
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before - 3 * PAYMENT,
        )

    def test_claim_exactly_at_the_current_block_has_nothing_to_claim(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=8, owner=owner_pk,
        )

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        _, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 8)
        # A failed tx keeps its fee, so the owner never gains from it.
        self.assertLessEqual(
            block.accounts.get_account(owner_pk, self.node).balance, 100_000,
        )

    def test_claim_one_block_before_the_current_block_pays_one(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=7, owner=owner_pk,
        )
        treasury_before = treasury.balance

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 8)
        self.assertEqual(
            block.accounts.get_account(owner_pk, self.node).balance,
            100_000 + PAYMENT - receipt.total_fee,
        )
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before - PAYMENT,
        )

    def test_claim_with_a_prepaid_pointer_has_nothing_to_claim(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=9, owner=owner_pk,
        )

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        _, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 9)

    # --- accounting scope ---

    def test_claim_on_a_secured_loan_records_no_write_off(self):
        # Pins the secured path only. An insured loan takes the separate
        # owner-initiated insurance claim path (Plan C Task 3), pinned in
        # ``test_treasury_insurance_claim``.
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
        )

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        user, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        # A secured claim collects interest; it is not a default write-off.
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user.defaulted, 0)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 0)

    # --- eligibility ---

    def test_claim_by_a_non_owner_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, _ = self._seed_owner(block)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
        )

        receipt = self._claim(block, stranger_pk, stranger_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(
            block.accounts.get_account(owner_pk, self.node).balance, 100_000,
        )

    def test_claim_by_the_treasury_owner_fails(self):
        block = self._make_block(height=8)
        claimant_pk, claimant_key = seed_sender_account(block, balance=1_000_000)
        _, loan_tx_id, _ = self._seed_loan(
            block, claimant_pk, paid_until=5, owner=TREASURY_ADDRESS,
        )

        receipt = self._claim(block, claimant_pk, claimant_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_claim_of_a_closed_loan_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=0, owner=owner_pk,
        )

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_claim_the_treasury_cannot_cover_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        treasury, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
            treasury_balance=3 * PAYMENT - 1,
        )
        treasury_before = treasury.balance

        receipt = self._claim(block, owner_pk, owner_key, loan_tx_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before,
        )
        _, loan = self._get_user_and_loan(block, borrower_pk, loan_tx_id)
        self.assertEqual(loan.paid_until_block, 5)

    def test_claim_handler_does_not_debit_the_treasury_before_the_owner_is_validated(self):
        # Direct call: ``apply`` would roll the debit back, hiding the order.
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk = os.urandom(32)  # no account seeded
        treasury, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
        )
        treasury_before = treasury.balance

        status = self._apply_claim_handler_only(block, owner_pk, loan_tx_id)

        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(
            block.accounts.get_account(TREASURY_ADDRESS, self.node).balance,
            treasury_before,
        )

    def test_claim_with_a_nonzero_amount_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = self._seed_owner(block)
        _, loan_tx_id, _ = self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
        )

        receipt = self._claim(
            block, owner_pk, owner_key, loan_tx_id, amount=1,
        )

        self.assertEqual(receipt.status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
