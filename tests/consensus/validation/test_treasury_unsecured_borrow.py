"""Validation tests for the unsecured TREASURY_BORROW (0x21) path.

An unsecured borrow assembles its principal by claiming one or more
guarantees (`docs/plans/archive/2026-09-21-limit-offers.md`) instead of posting
stake. The borrower receives ``discounted_amount - insurance_fee -
sum(price)``; the Treasury keeps the insurance fee; each claimed guarantee's
guarantor is paid its `price` and has its `guaranteed` increased by the
guarantee's `amount`.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.block.rate_window import windowed_rate_fraction
from astreum.consensus.transaction.treasury.discount import (
    calculate_discounted_amount,
)
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

STAKE = 1_000_000
CTF = 1000

INTERVAL = 4
COUNT = 2
DURATION = INTERVAL * COUNT  # 8 (pow2)


class TestTreasuryUnsecuredBorrow(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_stake=STAKE, cumulative_fee=CTF,
        )
        entries = self.prev_block.height.bit_length() or 1
        self.prev_block.statistics = [(CTF, STAKE, 0, 0)] * max(entries, 5)
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    def _discounted_amount(self, payment_amount):
        rate_frac = windowed_rate_fraction(self.block, DURATION)
        return calculate_discounted_amount(
            payment_amount=payment_amount,
            payment_interval_blocks=INTERVAL,
            payment_count=COUNT,
            rate_numerator=rate_frac[0],
            rate_denominator=rate_frac[1],
        )

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount, guarantee_refs, counter=0):
        return create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.UNSECURED, guarantee_refs=guarantee_refs,
            secret_key=sender_key,
        )

    def _seed_guarantee(self, *, guarantor_pk, guarantee_tx_id, guarantee_amount, price=0, expiry=1000,
                     duration=DURATION, total_interest_paid=None, guaranteed=0):
        if total_interest_paid is None:
            total_interest_paid = guarantee_amount
        guarantee = TreasuryGuarantee(amount=guarantee_amount, duration=duration, price=price, expiry=expiry)
        seed_guarantor_with_guarantee(
            self.node, self.treasury,
            guarantor=guarantor_pk, guarantee_transaction_id=guarantee_tx_id, guarantee=guarantee,
            total_interest_paid=total_interest_paid, guaranteed=guaranteed,
        )
        return guarantee

    # --- success ---

    def test_single_guarantee_covers_loan(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.assertIsNotNone(discounted)

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        price = 10
        guarantee = self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=discounted, price=price,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
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
        net_amount = discounted - price
        self.assertEqual(
            sender.balance,
            sender_before + net_amount - receipt.transaction_fee - receipt.storage_fee,
        )
        self.assertIsNotNone(guarantor)
        self.assertEqual(guarantor.balance, price)
        self.assertEqual(treasury.balance, (discounted + 1000) - discounted)

        borrower_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        borrower = TreasuryUserRecord.from_storage(self.node, borrower_head)
        self.assertIsNotNone(borrower)
        self.assertEqual(borrower.loaned, discounted)

        loans_trie = RadixTree(root_hash=bytes(borrower.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        loan = TreasuryLoanRecord.from_storage(self.node, loan_head)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.loan_type, LoanType.UNSECURED)
        self.assertEqual(loan.insurance_fee, 0)
        self.assertEqual(loan.claimed_guarantees, [(guarantor_pk, guarantee_tx_id, discounted)])

        guarantor_head = get_from_radix_tree(treasury.data, self.node, guarantor_pk)
        guarantor_record = TreasuryUserRecord.from_storage(self.node, guarantor_head)
        self.assertIsNotNone(guarantor_record)
        self.assertEqual(guarantor_record.guaranteed, discounted)

        self.assertEqual(self.block.global_loaned, discounted)
        self.assertEqual(self.block.global_loan_count, 1)

    def test_multiple_guarantees_from_different_guarantors(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_a = os.urandom(32)
        guarantor_b = os.urandom(32)
        guarantee_a_id = os.urandom(32)
        guarantee_b_id = os.urandom(32)
        half = discounted // 2
        remainder = discounted - half
        self._seed_guarantee(guarantor_pk=guarantor_a, guarantee_tx_id=guarantee_a_id, guarantee_amount=half, price=5)
        self._seed_guarantee(guarantor_pk=guarantor_b, guarantee_tx_id=guarantee_b_id, guarantee_amount=remainder, price=7)

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
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

    # --- failures ---

    def test_unknown_guarantee_ref_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        # Note: no guarantee ever seeded for guarantor_pk / guarantee_tx_id.
        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            guarantee_refs=[(guarantor_pk, os.urandom(32))],
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

    def test_mismatched_duration_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=discounted, duration=DURATION * 2,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_combined_guarantee_amount_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=discounted - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_guarantor_over_capacity_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        # total_interest_paid too low relative to the guarantee's amount.
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=discounted, total_interest_paid=discounted - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insurance_fee_deducted_when_network_has_history(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        self._seed_guarantee(guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id, guarantee_amount=discounted)

        # Seed network history: half the network's loans have defaulted.
        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 5_000

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        borrower_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        borrower = TreasuryUserRecord.from_storage(self.node, borrower_head)
        loans_trie = RadixTree(root_hash=bytes(borrower.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        loan = TreasuryLoanRecord.from_storage(self.node, loan_head)

        expected_fee = discounted * 5_000 // 10_000  # borrower_loaned=0 -> p = g
        self.assertEqual(loan.insurance_fee, expected_fee)
        self.assertGreater(loan.insurance_fee, 0)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance,
            sender_before + (discounted - expected_fee)
            - receipt.transaction_fee - receipt.storage_fee,
        )

    def test_net_amount_non_positive_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        guarantor_pk = os.urandom(32)
        guarantee_tx_id = os.urandom(32)
        # Price alone consumes the entire discounted amount.
        self._seed_guarantee(
            guarantor_pk=guarantor_pk, guarantee_tx_id=guarantee_tx_id,
            guarantee_amount=discounted, price=discounted,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
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
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.UNSECURED, guarantee_refs=[(guarantor_pk, guarantee_tx_id)],
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
