"""`CLAIM_COST` loans prepay through Plan 2's handler with no new path.

The rules under test (Plan B, Task 4):

- `TREASURY_REPAY` accepts a `CLAIM_COST` loan as it accepts every other
  loan type. Paying ``N * payment_amount`` advances ``paid_until_block`` by
  ``N``, credits the payment to the loan's `owner` (the treasury by
  default), and drops the sender's balance by the payment.
- Prepaid blocks accrue no service: after a prepay, settlement nets zero
  service on that loan and leaves its pointer alone.
- A prepay of a clean loan keeps `missed_count` and the write-off totals
  flat.
- A `CLAIM_COST` loan takes no write-off branch. Arrears stay on the pointer
  like a secured loan's arrears.
- The whole-multiple rule rejects a partial payment, and a closed loan
  (``paid_until_block == 0``) is rejected.

No source file changes in this task. The handler's only loan-type branch is
the `UNSECURED` write-off.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parents[2] / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.treasury.loans import (
    load_user_loan,
    settle_claim_payout,
)
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    load_user_record,
)

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
HEIGHT = 5


class TestClaimCostPrepay(unittest.TestCase):
    """The existing prepay handler serves `CLAIM_COST` loans unchanged."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block, height=HEIGHT)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _seed_loan(
        self,
        sender: bytes,
        *,
        paid_until_block: int = HEIGHT,
        payment_amount: int = PAYMENT,
        creation_block_number: int = HEIGHT,
    ) -> bytes:
        """Seed one open `CLAIM_COST` loan and return its trie key."""
        loan_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=creation_block_number,
            loan_type=LoanType.CLAIM_COST,
            discounted_amount=2_000,
            payment_amount=payment_amount,
            paid_until_block=paid_until_block,
        )
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=0
        )
        seed_user_with_loan(
            self.node,
            treasury,
            sender=sender,
            user_balance=0,
            loan_tx_id=loan_id,
            loan=loan,
        )
        return loan_id

    def _repay(self, sender: bytes, key: bytes, loan_id: bytes, amount: int):
        """Apply one `TREASURY_REPAY` transaction and flush its writes."""
        tx = create_transaction(
            chain_id=1,
            counter=0,
            sender=sender,
            recipient=TREASURY_ADDRESS,
            amount=amount,
            code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_id,
            secret_key=key,
        )
        apply_transaction(self.node, self.block, store_tx(self.node, tx))
        flush_pending(self.node, self.block)
        return self.block.receipts[-1]

    def _loan(self, borrower: bytes, loan_id: bytes) -> TreasuryLoanRecord:
        """Load one loan after flushing the block's pending expressions."""
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        loan = load_user_loan(
            node=self.node,
            treasury_account=treasury,
            borrower=borrower,
            loan_transaction_id=loan_id,
        )
        self.assertIsNotNone(loan)
        return loan

    def _user_record(self, borrower: bytes):
        """Load the borrower's treasury user record."""
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        return load_user_record(self.node, treasury.data, borrower)

    def _balance(self, address: bytes) -> int:
        """Return one account's balance."""
        account = self.block.accounts.get_account(address, self.node)
        return account.balance if account is not None else 0

    # ------------------------------------------------------------------
    # prepay through Plan 2's handler
    # ------------------------------------------------------------------

    def test_a_claim_cost_loan_prepays_through_the_existing_handler(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, 3 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(sender, loan_id)
        self.assertEqual(loan.loan_type, LoanType.CLAIM_COST)
        self.assertEqual(loan.paid_until_block, HEIGHT + 3)
        self.assertEqual(loan.missed_count, 0)
        # The whole payment is credited to the loan's owner, the treasury.
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + 3 * PAYMENT
        )
        # The payment leaves the sender, plus the cash fees of the tx.
        self.assertEqual(
            self._balance(sender),
            sender_before - 3 * PAYMENT - receipt.total_fee,
        )
        user_record = self._user_record(sender)
        self.assertEqual(user_record.total_interest_paid, 3 * PAYMENT)
        self.assertEqual(user_record.defaulted, 0)

    def test_prepaid_blocks_accrue_no_service_at_settlement(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        self.assertEqual(
            self._repay(sender, key, loan_id, 3 * PAYMENT).status, STATUS_SUCCESS
        )
        # The pointer runs three blocks ahead of the chain.
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT + 3)
        treasury_before = self._balance(TREASURY_ADDRESS)
        interest_before = self._user_record(sender).total_interest_paid

        # Settle at a height the prepay already covers.
        self.block.height = HEIGHT + 2
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        payout = 1_000_000
        result = settle_claim_payout(
            node=self.node,
            block=self.block,
            accounts=self.block.accounts,
            treasury_account=treasury,
            borrower=sender,
            payout=payout,
        )

        # No service is due, so the whole payout reaches the sender.
        self.assertEqual(result, payout)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        self.assertEqual(
            self._user_record(sender).total_interest_paid, interest_before
        )
        # The settled pointer is not pulled backwards to the height.
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT + 3)

    def test_missed_count_and_write_offs_stay_flat_through_a_prepay(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)

        receipt = self._repay(sender, key, loan_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(sender, loan_id)
        self.assertEqual(loan.missed_count, 0)
        user_record = self._user_record(sender)
        self.assertEqual(user_record.defaulted, 0)
        self.assertEqual(getattr(self.block, "global_defaulted", 0), 0)

    def test_claim_cost_arrears_are_not_written_off(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        # Three blocks of arrears: the pointer is below the current height.
        # The loan originated earlier, so the malformed-pointer guard does
        # not apply.
        loan_id = self._seed_loan(
            sender, paid_until_block=HEIGHT - 3, creation_block_number=0
        )

        receipt = self._repay(sender, key, loan_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(sender, loan_id)
        # Only the one paid block moves the pointer. No write-off to h.
        self.assertEqual(loan.paid_until_block, HEIGHT - 2)
        self.assertEqual(loan.missed_count, 0)
        user_record = self._user_record(sender)
        self.assertEqual(user_record.defaulted, 0)
        self.assertEqual(getattr(self.block, "global_defaulted", 0), 0)

    # ------------------------------------------------------------------
    # rejections stay shared with every loan type
    # ------------------------------------------------------------------

    def test_a_partial_payment_is_rejected(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, PAYMENT + 1)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        # Only the fees leave a rejected transaction.
        self.assertEqual(
            self._balance(sender), sender_before - receipt.total_fee
        )

    def test_a_closed_loan_is_rejected(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender, paid_until_block=0)

        receipt = self._repay(sender, key, loan_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)


if __name__ == "__main__":
    unittest.main()
