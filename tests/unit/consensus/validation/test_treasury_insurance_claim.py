"""Validation tests for the owner-initiated insurance claim (Plan C, Task 3).

An insured (``UNSECURED`` or ``CLAIM_COST``) loan with missed service is made
whole through ``TREASURY_CLAIM``:

- the missed amount ``overdue_blocks * payment_amount`` is simply PAID to the
  loan's owner as new money, like rent; nothing is debited from a treasury
  pot, so the supply level grows by the payout;
- the pointer advances to the current height;
- the payout is the write-off event: the borrower's ``missed_count`` grows by
  the overdue blocks, the borrower's ``defaulted`` and the block's
  ``total_written_off`` grows by the paid amount;
- initiation is permissionless: any sender may claim, pays the claim's own
  fees, and the payout always goes to the loan's owner;
- a loan owned by the treasury pays the treasury;
- a second claim with no arrears fails, and a closed loan fails;
- the secured owner-claim path is Plan 2 behavior and is pinned elsewhere.
"""

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
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.treasury.loans import load_user_loan
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
    load_user_record,
)
from astreum.expression import NIL, ZERO32, bytes_, link
from astreum.storage.radix import (
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
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)

PAYMENT = 100
PRINCIPAL = 450
# The key the deleted fund ledger used. No record may ever appear at it.
FUND_KEY = b"\xff" * 31 + b"\xfe"


def _claim_data(loan_tx_id):
    """Encode a claim's data: a one-element link-list holding the loan id."""
    return link(bytes_(loan_tx_id), NIL)


class TestTreasuryInsuranceClaim(unittest.TestCase):
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
        """Store a real borrow transaction; the loan is keyed by its hash."""
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
        loan_type=LoanType.UNSECURED,
        payment_amount=PAYMENT,
        principal=PRINCIPAL,
        insurance_interval_fee=0,
        user_balance=10_000,
        treasury_balance=0,
        user_defaulted=0,
        user_loaned=0,
    ):
        loan_tx_id = self._seed_loan_tx(borrower_pk)
        self._loan_id = loan_tx_id
        loan = TreasuryLoanRecord(
            creation_block_number=creation,
            loan_type=loan_type,
            discounted_amount=principal,
            payment_amount=payment_amount,
            paid_until_block=paid_until,
            insurance_interval_fee=insurance_interval_fee,
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
        # ``seed_user_with_loan`` writes a record with zero history; rewrite it
        # with a nonzero history when a test needs one.
        if user_defaulted or user_loaned:
            head = get_from_radix_tree(treasury.data, self.node, borrower_pk)
            record = TreasuryUserRecord.from_storage(self.node, head)
            updated = TreasuryUserRecord(
                balance=record.balance,
                loans_root_hash=record.loans_root_hash,
                total_interest_paid=record.total_interest_paid,
                loaned=user_loaned,
                defaulted=user_defaulted,
            )
            put_in_radix_tree(
                treasury.data, self.node, borrower_pk, updated.expr().hash()
            )
            self.node.hot_storage[updated.expr().hash()] = updated.expr()
            for trie_node in treasury.data.nodes.values():
                self.node.hot_storage[radix_node_hash(trie_node)] = (
                    get_radix_node_expr(trie_node)
                )
            treasury.data_hash = treasury.data.root_hash or ZERO32
            block.accounts.set_account(TREASURY_ADDRESS, treasury)
        return treasury, loan_tx_id, loan

    def _claim(self, block, claimant_pk, claimant_key, loan_tx_id, *, counter=0,
               amount=0):
        tx = create_transaction(
            chain_id=1, counter=counter, sender=claimant_pk,
            recipient=TREASURY_ADDRESS, amount=amount,
            code=TransactionCode.TREASURY_CLAIM,
            data=_claim_data(loan_tx_id), secret_key=claimant_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block.receipts[-1]

    def _loan(self, block, borrower_pk, loan_tx_id):
        flush_pending(self.node, block)
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        loan = load_user_loan(
            node=self.node,
            treasury_account=treasury,
            borrower=borrower_pk,
            loan_transaction_id=loan_tx_id,
        )
        self.assertIsNotNone(loan)
        return loan

    def _user_record(self, block, borrower_pk):
        flush_pending(self.node, block)
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        record = load_user_record(
            self.node, treasury.data, borrower_pk, contact=block.contact
        )
        self.assertIsNotNone(record)
        return record

    def _balance(self, block, address):
        account = block.accounts.get_account(address, self.node)
        return account.balance if account is not None else 0

    def _assert_no_fund(self, block):
        """No fund record exists anywhere in the treasury data trie."""
        flush_pending(self.node, block)
        treasury = block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertIsNone(
            get_from_radix_tree(treasury.data, self.node, FUND_KEY),
            "a fund record was written; an insurance payout is simply minted",
        )

    # --- the payout is new money to the owner ---

    def test_the_claim_pays_the_owner_the_missed_amount_as_new_money(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        treasury_seed = 50_000
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
            treasury_balance=treasury_seed,
        )
        supply_before = block.total_balance

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(block, owner_pk),
            100_000 + 3 * PAYMENT - receipt.total_fee,
        )
        # The treasury is never debited: the payout is minted, so its pot
        # keeps every unit it held.
        self.assertEqual(
            self._balance(block, TREASURY_ADDRESS), treasury_seed,
        )
        burned = receipt.total_fee - receipt.storage_fee
        self.assertEqual(
            block.total_balance - supply_before + burned, 3 * PAYMENT
        )
        self._assert_no_fund(block)

    def test_the_claim_advances_the_pointer_to_the_current_height(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(block, borrower_pk, paid_until=7, owner=owner_pk)

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8)
        self.assertEqual(
            self._balance(block, owner_pk),
            100_000 + PAYMENT - receipt.total_fee,
        )

    def test_the_payout_is_the_write_off_event(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
            user_defaulted=1_000, user_loaned=5_000,
        )

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        loan = self._loan(block, borrower_pk, self._loan_id)
        record = self._user_record(block, borrower_pk)
        self.assertEqual(loan.missed_count, 3)
        self.assertEqual(record.defaulted, 1_000 + 3 * PAYMENT)
        # The history that feeds the estimator is otherwise untouched.
        self.assertEqual(record.loaned, 5_000)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 3 * PAYMENT)

    def test_a_second_claim_with_no_arrears_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        self._seed_loan(block, borrower_pk, paid_until=5, owner=owner_pk)

        first = self._claim(block, owner_pk, owner_key, self._loan_id)
        self.assertEqual(first.status, STATUS_SUCCESS)
        owner_after_first = self._balance(block, owner_pk)
        global_after_first = (block.statistics.total_written_off if block.statistics else 0)

        second = self._claim(block, stranger_pk, stranger_key, self._loan_id)

        self.assertEqual(second.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, owner_pk), owner_after_first)
        loan = self._loan(block, borrower_pk, self._loan_id)
        self.assertEqual(loan.paid_until_block, 8)
        self.assertEqual(loan.missed_count, 3)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), global_after_first)

    def test_a_closed_loan_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(block, borrower_pk, paid_until=0, owner=owner_pk)

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, owner_pk), 100_000 - receipt.total_fee)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 0)

    def test_a_prepaid_pointer_has_nothing_to_claim(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(block, borrower_pk, paid_until=9, owner=owner_pk)

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._loan(block, borrower_pk, self._loan_id).paid_until_block, 9)
        self.assertEqual(self._balance(block, owner_pk), 100_000 - receipt.total_fee)

    def test_a_malformed_pointer_below_creation_fails_closed(self):
        # The insured write-off paths reject a pointer below the creation
        # block; a claim must not mint an unbounded payout from it.
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, creation=10, owner=owner_pk,
        )

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, owner_pk), 100_000 - receipt.total_fee)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 0)

    # --- same-block visibility ---

    def test_a_same_block_borrow_is_visible_to_the_claim(self):
        # The borrow writes a second loan and moves the borrower's loans trie
        # root. Those trie nodes are pending, not stored, so the claim must
        # read them from the block before it can find the overdue loan.
        block = self._make_block(height=8)
        borrower_pk, borrower_key = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(block, borrower_pk, paid_until=5, owner=owner_pk)

        borrow_tx = create_transaction(
            chain_id=1, counter=0, sender=borrower_pk,
            recipient=TREASURY_ADDRESS, amount=700,
            code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.UNSECURED, secret_key=borrower_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, borrow_tx))
        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        record = self._user_record(block, borrower_pk)
        # The claim sees the fresh borrow and keeps its history.
        self.assertEqual(record.loaned, 700)
        self.assertEqual(record.defaulted, 3 * PAYMENT)
        self.assertEqual(
            self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8
        )

    # --- permissionless initiation ---

    def test_anyone_may_initiate_and_the_payout_goes_to_the_owner(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, _ = seed_sender_account(block, balance=100_000)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        self._seed_loan(block, borrower_pk, paid_until=5, owner=owner_pk)

        receipt = self._claim(block, stranger_pk, stranger_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # The initiator pays the claim's own fees and receives nothing.
        self.assertEqual(
            self._balance(block, stranger_pk),
            1_000_000 - receipt.total_fee,
        )
        self.assertEqual(self._balance(block, owner_pk), 100_000 + 3 * PAYMENT)
        self.assertEqual(self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8)
        self.assertEqual(
            self._user_record(block, borrower_pk).defaulted, 3 * PAYMENT
        )
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 3 * PAYMENT)

    def test_the_payout_creates_the_owner_account_when_missing(self):
        # A permissionless claim must be able to make a real owner whole even
        # when the owner has no account yet. The mint creates the account.
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        owner_pk = b"\x42" * 32
        self._seed_loan(block, borrower_pk, paid_until=5, owner=owner_pk)

        receipt = self._claim(block, stranger_pk, stranger_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(block, owner_pk), 3 * PAYMENT)

    def test_a_loan_owned_by_the_treasury_pays_the_treasury(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=TREASURY_ADDRESS,
        )

        receipt = self._claim(block, stranger_pk, stranger_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(block, TREASURY_ADDRESS), 3 * PAYMENT)
        self.assertEqual(
            self._balance(block, stranger_pk),
            1_000_000 - receipt.total_fee,
        )
        self.assertEqual(self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8)
        self.assertEqual(
            self._user_record(block, borrower_pk).defaulted, 3 * PAYMENT
        )

    def test_a_treasury_owned_claim_cost_loan_pays_the_treasury(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        stranger_pk, stranger_key = seed_sender_account(block, balance=1_000_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=TREASURY_ADDRESS,
            loan_type=LoanType.CLAIM_COST, insurance_interval_fee=5,
        )

        receipt = self._claim(block, stranger_pk, stranger_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(self._balance(block, TREASURY_ADDRESS), 3 * PAYMENT)
        self.assertEqual(
            self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8
        )
        record = self._user_record(block, borrower_pk)
        self.assertEqual(record.defaulted, 3 * PAYMENT)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 3 * PAYMENT)

    # --- the claim-cost loan rides the same branch ---

    def test_a_claim_cost_loan_rides_the_insurance_branch(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
            loan_type=LoanType.CLAIM_COST, insurance_interval_fee=5,
        )

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self._balance(block, owner_pk),
            100_000 + 3 * PAYMENT - receipt.total_fee,
        )
        self.assertEqual(
            self._balance(block, TREASURY_ADDRESS),
            0,
            "the claim does not move the premium; only the minted payout moves",
        )
        self.assertEqual(self._loan(block, borrower_pk, self._loan_id).paid_until_block, 8)
        self.assertEqual(
            self._user_record(block, borrower_pk).defaulted, 3 * PAYMENT
        )
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 3 * PAYMENT)
        self._assert_no_fund(block)

    # --- guard failures leave no state behind ---

    def test_a_nonzero_amount_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(block, borrower_pk, paid_until=5, owner=owner_pk)

        receipt = self._claim(
            block, owner_pk, owner_key, self._loan_id, amount=1,
        )

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, owner_pk), 100_000 - receipt.total_fee)
        loan = self._loan(block, borrower_pk, self._loan_id)
        self.assertEqual(loan.paid_until_block, 5)
        self.assertEqual(loan.missed_count, 0)
        record = self._user_record(block, borrower_pk)
        self.assertEqual(record.defaulted, 0)
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 0)

    def test_a_zero_payment_amount_fails(self):
        block = self._make_block(height=8)
        borrower_pk, _ = seed_sender_account(block, balance=1_000_000)
        owner_pk, owner_key = seed_sender_account(block, balance=100_000)
        self._seed_loan(
            block, borrower_pk, paid_until=5, owner=owner_pk,
            payment_amount=0,
        )

        receipt = self._claim(block, owner_pk, owner_key, self._loan_id)

        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, owner_pk), 100_000 - receipt.total_fee)
        loan = self._loan(block, borrower_pk, self._loan_id)
        self.assertEqual(loan.paid_until_block, 5)
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(
            self._user_record(block, borrower_pk).defaulted, 0
        )
        self.assertEqual((block.statistics.total_written_off if block.statistics else 0), 0)


if __name__ == "__main__":
    unittest.main()
