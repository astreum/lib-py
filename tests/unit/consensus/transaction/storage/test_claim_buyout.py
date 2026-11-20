"""A `CLAIM_COST` loan buys out at the quote and ends.

The rules under test (Plan B, Task 5, Spec 3):

- A buyout pays ``ceil(principal / r_q)``, the Plan A quote, to the loan's
  ``owner``. The treasury is paid when it owns the loan, the buyer when the
  loan was sold.
- The loan ends with the closed sentinel ``paid_until_block = 0``. Nothing
  of it is owed after that: a later settlement nets no service, and a second
  buyout is rejected.
- The payment is the exact price: the receipt refunds the excess of the
  transaction amount over it, and a short amount fails without moving owner
  money.
- Only the borrower can buy out the debt. It is the borrower's personal
  debt: the loan is looked up in the sender's own treasury record, so a
  stranger's transaction finds no loan.
- A borrower who owns the loan pays himself. The price and the refund land
  on the same account object, so the buyout costs the cash fees only.
- A zero quote has no price, and the buyout fails closed: no money moves and
  the loan stays open.
- The buyout writes no arrears off: ``missed_count``, ``defaulted``, and the
  global write-off total stay flat, and ``total_interest_paid`` does not
  change. The price is a purchase, not interest.

Quote fixtures are hand-checkable. With ``r_bar = 1/4`` (``total_fees = 1``,
``cumulative_total_balance = 4``) at height 1, the quote is
``r_bar * (1 - 1/(1 + r_bar)) = 1/4 * 1/5 = 1/20``. At height 2 it is
``1/4 * (1 - (4/5)^2) = 9/100``.
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
from astreum.consensus.transaction.treasury.close import handle_treasury_close
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
    seed_account,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)

PRINCIPAL = 2_000
PAYMENT = 100
TREASURY_BALANCE = 100_000
# The quote at height 1 is 1/20, so the buyout is ceil(2000 * 20) = 40_000.
PRICE_AT_ONE = 40_000
# The quote at height 2 is 9/100, so the buyout is ceil(2000 * 100 / 9)
# = 22_223. The ceiling matters: 22_222 * 9 = 199_998.
PRICE_AT_TWO = 22_223


class TestClaimBuyout(unittest.TestCase):
    """A `CLAIM_COST` loan is bought out at the quote and closed."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_fee=1,
            cumulative_total_balance=4,
            total_tx_fees=1,
            total_tx_bytes=1,
        )
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _seed_loan(
        self,
        borrower: bytes,
        *,
        height: int = 1,
        paid_until_block: int | None = None,
        owner: bytes = TREASURY_ADDRESS,
        principal: int = PRINCIPAL,
    ) -> bytes:
        """Seed one open `CLAIM_COST` loan and return its trie key."""
        loan_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=height,
            loan_type=LoanType.CLAIM_COST,
            discounted_amount=principal,
            payment_amount=PAYMENT,
            paid_until_block=(
                height if paid_until_block is None else paid_until_block
            ),
            owner=owner,
        )
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=TREASURY_BALANCE
        )
        seed_user_with_loan(
            self.node,
            treasury,
            sender=borrower,
            user_balance=0,
            loan_tx_id=loan_id,
            loan=loan,
        )
        return loan_id

    def _close(
        self,
        sender: bytes,
        key,
        loan_id: bytes,
        amount: int,
        *,
        counter: int = 0,
    ):
        """Apply one `TREASURY_CLOSE` transaction and flush its writes."""
        tx = create_transaction(
            chain_id=1,
            counter=counter,
            sender=sender,
            recipient=TREASURY_ADDRESS,
            amount=amount,
            code=TransactionCode.TREASURY_CLOSE,
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
    # price and payee
    # ------------------------------------------------------------------

    def test_buyout_pays_the_quote_to_the_treasury_and_closes_the_loan(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(sender, loan_id)
        self.assertEqual(loan.loan_type, LoanType.CLAIM_COST)
        self.assertEqual(loan.paid_until_block, 0)
        # The record keeps its identity.
        self.assertEqual(loan.discounted_amount, PRINCIPAL)
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PRICE_AT_ONE
        )
        # The sender pays exactly the price plus the cash fees.
        self.assertEqual(
            self._balance(sender), sender_before - PRICE_AT_ONE - receipt.total_fee
        )

    def test_the_price_follows_the_quote_with_integer_ceil(self):
        self.block = make_block(self.node, self.prev_block, height=2)
        seed_storage_account(self.block)
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender, height=2)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_TWO)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PRICE_AT_TWO
        )

    def test_a_buyout_pays_a_bought_loans_owner_not_the_treasury(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        owner = os.urandom(32)
        seed_account(self.block, owner, balance=0)
        loan_id = self._seed_loan(sender, owner=owner)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(owner), PRICE_AT_ONE)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)

    def test_a_self_owned_loan_buyout_nets_to_the_fees(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        # The borrower bought his own loan back first. The buyout then pays
        # the price to the same account object that pays it.
        loan_id = self._seed_loan(sender, owner=sender)
        sender_before = self._balance(sender)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE + 5_000)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)
        # The price and the refund cancel: only the cash fees leave.
        self.assertEqual(
            self._balance(sender), sender_before - receipt.total_fee
        )

    def test_the_buyout_refunds_the_excess(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE + 5_000)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PRICE_AT_ONE
        )
        self.assertEqual(
            self._balance(sender), sender_before - PRICE_AT_ONE - receipt.total_fee
        )

    # ------------------------------------------------------------------
    # rejections
    # ------------------------------------------------------------------

    def test_a_buyout_below_the_quote_fails(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE - 1)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 1)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        # Only the fees leave a rejected transaction.
        self.assertEqual(
            self._balance(sender), sender_before - receipt.total_fee
        )

    def test_a_closed_loan_cannot_be_bought_out_twice(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        self.assertEqual(
            self._close(sender, key, loan_id, PRICE_AT_ONE).status, STATUS_SUCCESS
        )
        treasury_after = self._balance(TREASURY_ADDRESS)
        sender_after = self._balance(sender)

        receipt = self._close(sender, key, loan_id, PRICE_AT_ONE, counter=1)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_after)
        self.assertEqual(self._balance(sender), sender_after - receipt.total_fee)

    def test_a_stranger_cannot_buy_out_another_accounts_loan(self):
        borrower, _ = seed_sender_account(self.block, balance=1_000_000)
        stranger, stranger_key = seed_sender_account(
            self.block, balance=1_000_000
        )
        loan_id = self._seed_loan(borrower)
        stranger_before = self._balance(stranger)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(stranger, stranger_key, loan_id, PRICE_AT_ONE)

        self.assertEqual(receipt.status, STATUS_FAILED)
        # The debt is the borrower's: the loan stays open and untouched.
        self.assertEqual(self._loan(borrower, loan_id).paid_until_block, 1)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        self.assertEqual(
            self._balance(stranger), stranger_before - receipt.total_fee
        )

    def test_a_zero_quote_fails_closed_without_moving_money(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)
        # The quote is zero at height 0, so the loan has no price. The apply
        # path cannot price a zero-quote block (storage pricing raises), so
        # this calls the handler directly to pin its fail-closed rule.
        self.block.height = 0
        tx = create_transaction(
            chain_id=1,
            counter=0,
            sender=sender,
            recipient=TREASURY_ADDRESS,
            amount=PRICE_AT_ONE,
            code=TransactionCode.TREASURY_CLOSE,
            loan_transaction_id=loan_id,
            secret_key=key,
        )

        status = handle_treasury_close(
            node=self.node, block=self.block, transaction=tx
        )

        self.assertEqual(status, STATUS_FAILED)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 1)
        self.assertEqual(self._balance(sender), sender_before)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)

    # ------------------------------------------------------------------
    # nothing remains owed
    # ------------------------------------------------------------------

    def test_a_bought_out_loan_accrues_no_service_at_settlement(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        self.assertEqual(
            self._close(sender, key, loan_id, PRICE_AT_ONE).status, STATUS_SUCCESS
        )
        treasury_before = self._balance(TREASURY_ADDRESS)
        interest_before = self._user_record(sender).total_interest_paid

        # A claim settled long after the buyout finds no open loan to net.
        self.block.height = 100
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

        self.assertEqual(result, payout)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, 0)
        self.assertEqual(
            self._user_record(sender).total_interest_paid, interest_before
        )

    def test_the_buyout_writes_nothing_off(self):
        self.block = make_block(self.node, self.prev_block, height=2)
        seed_storage_account(self.block)
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        # One block of arrears: the pointer is below the current height. The
        # buyout ends the loan without a write-off.
        loan_id = self._seed_loan(
            sender, height=1, paid_until_block=1
        )
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._close(sender, key, loan_id, PRICE_AT_TWO)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(sender, loan_id)
        user_record = self._user_record(sender)
        # The owner receives exactly the price: no arrears are added to it.
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PRICE_AT_TWO
        )
        self.assertEqual(
            self._balance(sender), sender_before - PRICE_AT_TWO - receipt.total_fee
        )
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(user_record.defaulted, 0)
        self.assertEqual(user_record.total_interest_paid, 0)
        self.assertEqual(getattr(self.block, "global_defaulted", 0), 0)


if __name__ == "__main__":
    unittest.main()
