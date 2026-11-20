"""Per-interval insurance premiums on insured loans (Plan C, Task 2).

The rules under test (Spec 4, Premiums; Plan C, Task 2; the design owner's
no-fund ruling):

- An insured loan carries ``insurance_interval_fee``: one premium per block
  of service settled. `UNSECURED` (self-taken) and `CLAIM_COST` loans are
  insured; a `SECURED` loan pays no premium.
- The premium is collected with the service, from the borrower:
  - on the repay path, the borrower pays ``blocks * fee`` on top of the
    payment;
  - on the claim-netting path, the premium comes out of the claim payout
    after the service, and the loan owner still receives the whole service.
- Premiums are treasury income, like fees: they are credited to the treasury
  account's balance. THERE IS NO FUND: no ledger write, no sub-account key,
  no separate record anywhere in the treasury data trie.
- The premium rate comes from the existing smoothed estimator at origination.
  Nothing derives a rate from a ledger, because no ledger exists.
- A zero-fee loan collects nothing.
- A payout that covers the service but not the premium settles nothing,
  like a payout that cannot cover the service.

The premium fee value itself is set at origination (self-taken and claim-cost
alike: the measured estimator). These tests plant the fee on the loan record
to test the collection points only.
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

from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_SUCCESS
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
from astreum.storage.radix import get_from_radix_tree

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

PAYMENT = 100
FEE = 7
HEIGHT = 20
# The key the deleted fund ledger used. No record may ever appear at it.
FUND_KEY = b"\xff" * 31 + b"\xfe"


class TestTreasuryIntervalInsurance(unittest.TestCase):
    """The premium is treasury income; no fund exists."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=1_000_000,
            cumulative_fee=1000,
            total_tx_fees=1,
            total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, self.prev_block, height=HEIGHT)
        seed_storage_account(self.block)

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------

    def _seed_loan(
        self,
        borrower: bytes,
        *,
        loan_type: LoanType = LoanType.UNSECURED,
        insurance_interval_fee: int = FEE,
        paid_until_block: int = HEIGHT,
        creation_block_number: int = HEIGHT,
        owner: bytes = TREASURY_ADDRESS,
    ) -> bytes:
        """Seed one open loan on a fresh treasury and return its id."""
        loan_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=creation_block_number,
            loan_type=loan_type,
            discounted_amount=2_000,
            payment_amount=PAYMENT,
            paid_until_block=paid_until_block,
            insurance_interval_fee=insurance_interval_fee,
            owner=owner,
        )
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=0
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

    def _repay(self, sender: bytes, key, loan_id: bytes, amount: int):
        """Apply one repay and flush its writes."""
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

    def _treasury(self):
        return self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

    def _loan(self, borrower: bytes, loan_id: bytes) -> TreasuryLoanRecord:
        flush_pending(self.node, self.block)
        loan = load_user_loan(
            node=self.node,
            treasury_account=self._treasury(),
            borrower=borrower,
            loan_transaction_id=loan_id,
        )
        self.assertIsNotNone(loan)
        return loan

    def _assert_no_fund(self):
        """No fund record exists anywhere in the treasury data trie."""
        flush_pending(self.node, self.block)
        self.assertIsNone(
            get_from_radix_tree(self._treasury().data, self.node, FUND_KEY),
            "a fund record was written; premiums are treasury income only",
        )

    def _balance(self, address: bytes) -> int:
        account = self.block.accounts.get_account(address, self.node)
        return account.balance if account is not None else 0

    def _settle(self, borrower: bytes, payout: int) -> int:
        treasury = self._treasury()
        return settle_claim_payout(
            node=self.node,
            block=self.block,
            accounts=self.block.accounts,
            treasury_account=treasury,
            borrower=borrower,
            payout=payout,
        )

    # ------------------------------------------------------------------
    # the repay path
    # ------------------------------------------------------------------

    def test_repay_credits_one_premium_per_paid_block_to_the_treasury(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, 3 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT + 3)
        premium = 3 * FEE
        # The owner side (the treasury) receives the service plus the
        # premium; the borrower pays both on top of the cash fees.
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + 3 * PAYMENT + premium
        )
        self.assertEqual(
            self._balance(sender),
            sender_before - 3 * PAYMENT - premium - receipt.total_fee,
        )
        self._assert_no_fund()

    def test_repay_pays_a_non_treasury_owner_the_service_and_the_treasury_the_premium(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        owner = os.urandom(32)
        seed_account(self.block, owner, balance=0)
        loan_id = self._seed_loan(sender, owner=owner)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, 2 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(owner), 2 * PAYMENT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before + 2 * FEE)
        self._assert_no_fund()

    def test_repay_of_a_zero_fee_loan_collects_nothing(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(sender, insurance_interval_fee=0)
        sender_before = self._balance(sender)
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, 2 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + 2 * PAYMENT
        )
        self.assertEqual(
            self._balance(sender), sender_before - 2 * PAYMENT - receipt.total_fee
        )
        self._assert_no_fund()

    def test_repay_of_a_secured_loan_collects_no_premium(self):
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(
            sender, loan_type=LoanType.SECURED, insurance_interval_fee=FEE
        )
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # The service only: the secured type is not insured.
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PAYMENT
        )
        self._assert_no_fund()

    def test_repay_from_arrears_pays_the_premium_only_on_paid_blocks(self):
        # The pointer sits five blocks behind, so five blocks are written off
        # and only the two paid blocks are settled. The written-off blocks buy
        # no service, so they pay no premium.
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(
            sender,
            paid_until_block=HEIGHT - 5,
            creation_block_number=0,
        )
        treasury_before = self._balance(TREASURY_ADDRESS)
        sender_before = self._balance(sender)

        receipt = self._repay(sender, key, loan_id, 2 * PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        premium = 2 * FEE
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + 2 * PAYMENT + premium
        )
        self.assertEqual(
            self._balance(sender),
            sender_before - 2 * PAYMENT - premium - receipt.total_fee,
        )
        loan = self._loan(sender, loan_id)
        self.assertEqual(loan.missed_count, 5)
        self.assertEqual(loan.paid_until_block, HEIGHT + 2)
        user_record = load_user_record(
            self.node, self._treasury().data, sender, contact=self.block.contact
        )
        self.assertEqual(user_record.defaulted, 5 * PAYMENT)
        self._assert_no_fund()

    def test_a_claim_cost_loan_repays_and_pays_its_premium(self):
        # The repay premium predicate covers every non-secured type, so a
        # claim-cost loan with a fee pays it through the repay path.
        sender, key = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(
            sender, loan_type=LoanType.CLAIM_COST, insurance_interval_fee=FEE
        )
        treasury_before = self._balance(TREASURY_ADDRESS)

        receipt = self._repay(sender, key, loan_id, PAYMENT)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(TREASURY_ADDRESS), treasury_before + PAYMENT + FEE
        )
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT + 1)
        self._assert_no_fund()

    # ------------------------------------------------------------------
    # the claim-netting path
    # ------------------------------------------------------------------

    def test_netting_credits_one_premium_per_settled_block_to_the_treasury(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        owner = os.urandom(32)
        seed_account(self.block, owner, balance=0)
        loan_id = self._seed_loan(
            sender,
            paid_until_block=HEIGHT - 4,
            creation_block_number=0,
            owner=owner,
        )
        treasury_before = self._balance(TREASURY_ADDRESS)
        payout = 1_000

        credited = self._settle(sender, payout)

        # Four blocks settle: service 400 to the owner, premium 28 to the
        # treasury as income.
        self.assertEqual(credited, payout - 4 * PAYMENT - 4 * FEE)
        self.assertEqual(self._balance(owner), 4 * PAYMENT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before + 4 * FEE)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)
        self._assert_no_fund()

    def test_netting_a_claim_cost_loan_credits_its_premium(self):
        # Claim-cost origination prices its fee with the same estimator; the
        # collection point charges the fee the record carries.
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        owner = os.urandom(32)
        seed_account(self.block, owner, balance=0)
        loan_id = self._seed_loan(
            sender,
            loan_type=LoanType.CLAIM_COST,
            paid_until_block=HEIGHT - 3,
            creation_block_number=0,
            owner=owner,
        )
        treasury_before = self._balance(TREASURY_ADDRESS)

        credited = self._settle(sender, 1_000)

        self.assertEqual(credited, 1_000 - 3 * PAYMENT - 3 * FEE)
        self.assertEqual(self._balance(owner), 3 * PAYMENT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before + 3 * FEE)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)
        self._assert_no_fund()

    def test_netting_a_zero_fee_loan_collects_nothing(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        owner = os.urandom(32)
        seed_account(self.block, owner, balance=0)
        loan_id = self._seed_loan(
            sender,
            insurance_interval_fee=0,
            paid_until_block=HEIGHT - 2,
            creation_block_number=0,
            owner=owner,
        )
        treasury_before = self._balance(TREASURY_ADDRESS)

        credited = self._settle(sender, 1_000)

        self.assertEqual(credited, 1_000 - 2 * PAYMENT)
        self.assertEqual(self._balance(owner), 2 * PAYMENT)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT)
        self._assert_no_fund()

    def test_a_payout_short_of_service_and_premium_settles_nothing(self):
        sender, _ = seed_sender_account(self.block, balance=1_000_000)
        loan_id = self._seed_loan(
            sender,
            insurance_interval_fee=50,
            paid_until_block=HEIGHT - 2,
            creation_block_number=0,
        )
        # Two blocks: service 200, premium 100. 250 covers the service only.
        payout = 250
        treasury_before = self._balance(TREASURY_ADDRESS)

        credited = self._settle(sender, payout)

        self.assertEqual(credited, payout)
        self.assertEqual(self._balance(TREASURY_ADDRESS), treasury_before)
        self.assertEqual(self._loan(sender, loan_id).paid_until_block, HEIGHT - 2)
        self._assert_no_fund()

    # ------------------------------------------------------------------
    # accumulation
    # ------------------------------------------------------------------

    def test_premiums_from_two_borrowers_in_one_block_accumulate_in_the_treasury(self):
        first, first_key = seed_sender_account(self.block, balance=1_000_000)
        second, second_key = seed_sender_account(self.block, balance=1_000_000)
        loan_a = os.urandom(32)
        loan_b = os.urandom(32)
        treasury = seed_treasury_account(self.node, self.block, treasury_balance=0)
        seed_user_with_loan(
            self.node,
            treasury,
            sender=first,
            user_balance=0,
            loan_tx_id=loan_a,
            loan=TreasuryLoanRecord(
                creation_block_number=HEIGHT,
                loan_type=LoanType.UNSECURED,
                discounted_amount=2_000,
                payment_amount=PAYMENT,
                paid_until_block=HEIGHT,
                insurance_interval_fee=FEE,
            ),
        )
        seed_user_with_loan(
            self.node,
            treasury,
            sender=second,
            user_balance=0,
            loan_tx_id=loan_b,
            loan=TreasuryLoanRecord(
                creation_block_number=HEIGHT,
                loan_type=LoanType.UNSECURED,
                discounted_amount=2_000,
                payment_amount=PAYMENT,
                paid_until_block=HEIGHT,
                insurance_interval_fee=FEE,
            ),
        )

        for sender, key, loan_id in (
            (first, first_key, loan_a),
            (second, second_key, loan_b),
        ):
            tx = create_transaction(
                chain_id=1,
                counter=0,
                sender=sender,
                recipient=TREASURY_ADDRESS,
                amount=PAYMENT,
                code=TransactionCode.TREASURY_REPAY,
                loan_transaction_id=loan_id,
                secret_key=key,
            )
            apply_transaction(self.node, self.block, store_tx(self.node, tx))

        self.assertEqual(
            [receipt.status for receipt in self.block.receipts],
            [STATUS_SUCCESS, STATUS_SUCCESS],
        )
        # Two services plus two premiums: both are treasury income here.
        self.assertEqual(self._balance(TREASURY_ADDRESS), 2 * PAYMENT + 2 * FEE)
        self._assert_no_fund()


if __name__ == "__main__":
    unittest.main()
