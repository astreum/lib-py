"""Validation tests for the unsecured TREASURY_BORROW (0x21) path.

An unsecured perpetual borrow assembles its principal ``L = transaction.amount``
by claiming one or more guarantees instead of posting stake. The borrower
receives ``L - insurance_fee - sum(price)``; the Treasury keeps the insurance
fee, pays the principal less the fee from its own balance, and each claimed
guarantee's guarantor is paid its `price` and has its `guaranteed` increased by
the guarantee's `amount`. The locked payment is ``ceil(L * r_bar)``.
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
    TreasuryGuarantee,
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
    seed_guarantor_with_guarantee,
    seed_storage_account,
    seed_treasury_account,
    store_tx,
)

# r_bar = 250_000 / 1_000_000 = 1/4, so payment = ceil(L / 4).
STAKE = 1_000_000
CTF = 250_000


def expected_payment(principal: int, fee: int = CTF, stake: int = STAKE) -> int:
    """Return the plan's per-block payment for *principal* at this rate."""
    return (principal * fee + stake - 1) // stake


def expected_insurance_fee(
    *, principal: int, loaned: int = 0, defaulted: int = 0,
    global_loan_count: int, global_loaned: int, global_defaulted: int,
) -> int | None:
    """Return the ceil-form insurance fee the plan requires, or None."""
    if global_loan_count <= 0:
        return 0
    denominator = loaned * global_loan_count + global_loaned
    if denominator <= 0:
        return None
    numerator = principal * (defaulted * global_loan_count + global_defaulted)
    return (numerator + denominator - 1) // denominator


class TestTreasuryUnsecuredBorrow(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=STAKE, total_balance=STAKE,
            cumulative_fee=CTF, total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount, guarantee_refs, counter=0):
        return create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.UNSECURED, guarantee_refs=guarantee_refs,
            secret_key=sender_key,
        )

    def _seed_guarantee(self, *, guarantor_pk, guarantee_tx_id, guarantee_amount, price=0,
                        expiry=1000, total_interest_paid=None, guaranteed=0):
        if total_interest_paid is None:
            total_interest_paid = guarantee_amount
        guarantee = TreasuryGuarantee(amount=guarantee_amount, price=price, expiry=expiry)
        seed_guarantor_with_guarantee(
            self.node, self.treasury,
            guarantor=guarantor_pk, guarantee_transaction_id=guarantee_tx_id, guarantee=guarantee,
            total_interest_paid=total_interest_paid, guaranteed=guaranteed,
        )
        return guarantee

    def _read_loan(self, sender_pk, tx_hash):
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        borrower_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        borrower = TreasuryUserRecord.from_storage(self.node, borrower_head)
        self.assertIsNotNone(borrower)
        loans_trie = RadixTree(root_hash=bytes(borrower.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        return borrower, TreasuryLoanRecord.from_storage(self.node, loan_head)

    # --- success ---

    def test_single_guarantee_covers_loan(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        price = 10
        guarantee = self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal, price=price,
        )
        self.assertFalse(hasattr(guarantee, "duration"))

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        guarantor = self.block.accounts.get_account(guarantor_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

        # First-ever unsecured loan on the network -> insurance_fee == 0.
        net_amount = principal - price
        self.assertEqual(
            sender.balance,
            sender_before + net_amount - receipt.total_fee,
        )
        self.assertIsNotNone(guarantor)
        self.assertEqual(guarantor.balance, price)
        # The treasury pays exactly the principal (less a zero insurance fee).
        self.assertEqual(treasury.balance, (principal + 1000) - principal)

        borrower, loan = self._read_loan(sender_pk, tx_hash)
        self.assertIsNotNone(loan)
        self.assertEqual(borrower.loaned, principal)
        self.assertEqual(loan.loan_type, LoanType.UNSECURED)
        self.assertEqual(loan.discounted_amount, principal)
        self.assertEqual(loan.payment_amount, expected_payment(principal))
        self.assertEqual(loan.paid_until_block, self.block.height)
        self.assertEqual(loan.insurance_fee, 0)
        self.assertEqual(loan.claimed_guarantees, [(guarantor_pk, guarantee_tx_id, principal)])
        self.assertEqual(len(loan.claimed_guarantees[0]), 3)  # no duration field

        guarantor_head = get_from_radix_tree(treasury.data, self.node, guarantor_pk)
        guarantor_record = TreasuryUserRecord.from_storage(self.node, guarantor_head)
        self.assertIsNotNone(guarantor_record)
        self.assertEqual(guarantor_record.guaranteed, principal)

        self.assertEqual(self.block.global_loaned, principal)
        self.assertEqual(self.block.global_loan_count, 1)

    def test_multiple_guarantees_from_different_guarantors(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_a = os.urandom(32)
        guarantor_b = os.urandom(32)
        guarantee_a_id = os.urandom(32)
        guarantee_b_id = os.urandom(32)
        half = principal // 2
        remainder = principal - half
        self._seed_guarantee(guarantor_pk=guarantor_a, guarantee_tx_id=guarantee_a_id, guarantee_amount=half, price=5)
        self._seed_guarantee(guarantor_pk=guarantor_b, guarantee_tx_id=guarantee_b_id, guarantee_amount=remainder, price=7)

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_a, guarantee_a_id), (guarantor_b, guarantee_b_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        guarantor_a_acct = self.block.accounts.get_account(guarantor_a, self.node)
        guarantor_b_acct = self.block.accounts.get_account(guarantor_b, self.node)
        self.assertEqual(guarantor_a_acct.balance, 5)
        self.assertEqual(guarantor_b_acct.balance, 7)

        guarantor_a_head = get_from_radix_tree(treasury.data, self.node, guarantor_a)
        guarantor_a_record = TreasuryUserRecord.from_storage(self.node, guarantor_a_head)
        self.assertEqual(guarantor_a_record.guaranteed, half)

        guarantor_b_head = get_from_radix_tree(treasury.data, self.node, guarantor_b)
        guarantor_b_record = TreasuryUserRecord.from_storage(self.node, guarantor_b_head)
        self.assertEqual(guarantor_b_record.guaranteed, remainder)

    def test_insurance_fee_rounds_up(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal,
        )

        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 3_333

        fee = expected_insurance_fee(
            principal=principal, global_loan_count=10,
            global_loaned=10_000, global_defaulted=3_333,
        )
        self.assertEqual(fee, 34)  # ceil(33.33), not floor(33.33)

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertEqual(loan.insurance_fee, fee)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance,
            sender_before + (principal - fee) - receipt.total_fee,
        )

    def test_treasury_covers_the_principal_less_the_insurance_fee(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        fee = expected_insurance_fee(
            principal=principal, global_loan_count=10,
            global_loaned=10_000, global_defaulted=3_333,
        )
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal - fee,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal,
        )

        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 3_333

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.balance, 0)

    # --- failures ---

    def test_unknown_guarantee_ref_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        # Note: no guarantee ever seeded for guarantor_pk / guarantee_tx_id.
        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, os.urandom(32))],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_expired_guarantee_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal, expiry=self.block.height,
        )
        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_duplicate_ref_rejected_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        with self.assertRaises(ValueError):
            self._make_borrow_tx(
                sender_pk, sender_key, amount=100,
                guarantee_refs=[(guarantor_pk, guarantee_tx_id), (guarantor_pk, guarantee_tx_id)],
            )

    def test_empty_guarantee_refs_rejected_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        with self.assertRaises(ValueError):
            self._make_borrow_tx(sender_pk, sender_key, amount=100, guarantee_refs=[])

    def test_insufficient_combined_guarantee_amount_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_guarantor_over_capacity_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        # total_interest_paid too low relative to the guarantee's amount.
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal, total_interest_paid=principal - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insurance_fee_must_be_less_than_principal(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal,
        )

        # Every loaned unit defaulted: p = 1, so I = L.
        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 10_000

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_treasury_below_principal_less_fee_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        fee = expected_insurance_fee(
            principal=principal, global_loan_count=10,
            global_loaned=10_000, global_defaulted=3_333,
        )
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal - fee - 1,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal,
        )

        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 3_333

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_net_amount_non_positive_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        # Price alone consumes the entire principal.
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=principal, price=principal,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=principal,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.UNSECURED, guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
