"""Validation tests for the unsecured TREASURY_BORROW (0x21) path.

An unsecured borrow assembles its principal by claiming one or more limit
offers (`docs/plans/archive/2026-09-21-limit-offers.md`) instead of posting
stake. The borrower receives ``discounted_amount - insurance_fee -
sum(price)``; the Treasury keeps the insurance fee; each claimed offer's
seller is paid its `price` and has its `sold_limit` increased by the
offer's `limit`.
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
    TreasuryCreditOffer,
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
    seed_seller_with_offer,
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

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount, offer_refs, counter=0):
        return create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.UNSECURED, offer_refs=offer_refs,
            secret_key=sender_key,
        )

    def _seed_offer(self, *, seller_pk, offer_tx_id, limit, price=0, expiry=1000,
                     duration=DURATION, total_interest_paid=None, sold_limit=0):
        if total_interest_paid is None:
            total_interest_paid = limit
        offer = TreasuryCreditOffer(limit=limit, duration=duration, price=price, expiry=expiry)
        seed_seller_with_offer(
            self.node, self.treasury,
            seller=seller_pk, offer_transaction_id=offer_tx_id, offer=offer,
            total_interest_paid=total_interest_paid, sold_limit=sold_limit,
        )
        return offer

    # --- success ---

    def test_single_offer_covers_loan(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.assertIsNotNone(discounted)

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        price = 10
        offer = self._seed_offer(
            seller_pk=seller_pk, offer_tx_id=offer_tx_id,
            limit=discounted, price=price,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        seller = self.block.accounts.get_account(seller_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

        # First-ever unsecured loan on the network -> insurance_fee == 0.
        net_amount = discounted - price
        self.assertEqual(
            sender.balance,
            sender_before + net_amount - receipt.transaction_fee - receipt.storage_fee,
        )
        self.assertIsNotNone(seller)
        self.assertEqual(seller.balance, price)
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
        self.assertEqual(loan.claimed_offers, [(seller_pk, offer_tx_id, discounted)])

        seller_head = get_from_radix_tree(treasury.data, self.node, seller_pk)
        seller_record = TreasuryUserRecord.from_storage(self.node, seller_head)
        self.assertIsNotNone(seller_record)
        self.assertEqual(seller_record.sold_limit, discounted)

        self.assertEqual(self.block.global_loaned, discounted)
        self.assertEqual(self.block.global_loan_count, 1)

    def test_multiple_offers_from_different_sellers(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)

        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_a = os.urandom(32)
        seller_b = os.urandom(32)
        offer_a_id = os.urandom(32)
        offer_b_id = os.urandom(32)
        half = discounted // 2
        remainder = discounted - half
        self._seed_offer(seller_pk=seller_a, offer_tx_id=offer_a_id, limit=half, price=5)
        self._seed_offer(seller_pk=seller_b, offer_tx_id=offer_b_id, limit=remainder, price=7)

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_a, offer_a_id), (seller_b, offer_b_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        seller_a_acct = self.block.accounts.get_account(seller_a, self.node)
        seller_b_acct = self.block.accounts.get_account(seller_b, self.node)
        self.assertEqual(seller_a_acct.balance, 5)
        self.assertEqual(seller_b_acct.balance, 7)

        seller_a_head = get_from_radix_tree(treasury.data, self.node, seller_a)
        seller_a_record = TreasuryUserRecord.from_storage(self.node, seller_a_head)
        self.assertEqual(seller_a_record.sold_limit, half)

        seller_b_head = get_from_radix_tree(treasury.data, self.node, seller_b)
        seller_b_record = TreasuryUserRecord.from_storage(self.node, seller_b_head)
        self.assertEqual(seller_b_record.sold_limit, remainder)

    # --- failures ---

    def test_unknown_offer_ref_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_pk = os.urandom(32)
        # Note: no offer ever seeded for seller_pk / offer_tx_id.
        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, os.urandom(32))],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_duplicate_ref_rejected_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        with self.assertRaises(ValueError):
            self._make_borrow_tx(
                sender_pk, sender_key, amount=100,
                offer_refs=[(seller_pk, offer_tx_id), (seller_pk, offer_tx_id)],
            )

    def test_empty_offer_refs_rejected_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        with self.assertRaises(ValueError):
            self._make_borrow_tx(sender_pk, sender_key, amount=100, offer_refs=[])

    def test_mismatched_duration_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        self._seed_offer(
            seller_pk=seller_pk, offer_tx_id=offer_tx_id,
            limit=discounted, duration=DURATION * 2,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_combined_limit_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        self._seed_offer(
            seller_pk=seller_pk, offer_tx_id=offer_tx_id,
            limit=discounted - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_seller_over_capacity_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        discounted = self._discounted_amount(payment_amount)
        self.treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=discounted + 1000,
        )
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        # total_interest_paid too low relative to the offer's limit.
        self._seed_offer(
            seller_pk=seller_pk, offer_tx_id=offer_tx_id,
            limit=discounted, total_interest_paid=discounted - 1,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
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
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        self._seed_offer(seller_pk=seller_pk, offer_tx_id=offer_tx_id, limit=discounted)

        # Seed network history: half the network's loans have defaulted.
        self.block.global_loan_count = 10
        self.block.global_loaned = 10_000
        self.block.global_defaulted = 5_000

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
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
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        # Price alone consumes the entire discounted amount.
        self._seed_offer(
            seller_pk=seller_pk, offer_tx_id=offer_tx_id,
            limit=discounted, price=discounted,
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
            offer_refs=[(seller_pk, offer_tx_id)],
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        seller_pk = os.urandom(32)
        offer_tx_id = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_BORROW,
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.UNSECURED, offer_refs=[(seller_pk, offer_tx_id)],
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
