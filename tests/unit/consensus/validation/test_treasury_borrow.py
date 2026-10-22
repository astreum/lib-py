"""Validation tests for the TREASURY_BORROW (0x21) transaction code.

A borrow creates a secured loan against the sender's treasury stake. The
sender receives the discounted (present-value) amount upfront and owes
``payment_amount * payment_count`` in scheduled payments. The per-block rate
comes from ``block_rate_fraction`` (previous block cumulative totals).
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
from astreum.consensus.block.rate_window import windowed_rate_fraction
from astreum.consensus.transaction.treasury.discount import (
    calculate_discounted_amount,
)
from astreum.consensus.transaction.treasury.record import (
    LoanType,
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
    seed_storage_account,
    seed_treasury_account,
    store_tx,
)

# Tuned so the per-block rate is small (denom/stake tiny) → discount ≈ total,
# while storage fees (stake/denom) stay modest for a funded sender.
STAKE = 1_000_000
CTF = 1000  # cumulative_transaction_fee


INTERVAL = 4
COUNT = 2
DURATION = INTERVAL * COUNT  # 8 (pow2)


class TestTreasuryBorrow(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_stake=STAKE, cumulative_fee=CTF,
        )
        # Seed statistics so range 8 (idx 3) is available.
        entries = self.prev_block.height.bit_length() or 1
        self.prev_block.statistics = [(CTF, STAKE, 0, 0)] * max(entries, 5)
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount):
        return create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.SECURED, secret_key=sender_key,
        )

    # --- success ---

    def test_borrow_credits_discounted_amount_and_creates_loan(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        scheduled_total = payment_amount * COUNT  # 200

        rate_frac = windowed_rate_fraction(self.block, DURATION)
        discounted = calculate_discounted_amount(
            payment_amount=payment_amount,
            payment_interval_blocks=INTERVAL,
            payment_count=COUNT,
            rate_numerator=rate_frac[0],
            rate_denominator=rate_frac[1],
        )
        self.assertIsNotNone(discounted)
        self.assertGreater(discounted, 0)

        record = TreasuryUserRecord(
            balance=scheduled_total + 1000,  # collateral >= scheduled_total
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=discounted + 1000,
            user_records={sender_pk: record},
        )

        tx = self._make_borrow_tx(
            sender_pk, sender_key, amount=payment_amount,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # Sender credited the discounted amount (minus fees).
        self.assertEqual(
            sender.balance,
            sender_before + discounted - receipt.transaction_fee - receipt.storage_fee,
        )
        # Treasury debited the discounted amount.
        self.assertEqual(treasury.balance, (discounted + 1000) - discounted)

        # Loan record exists in the user's loans trie.
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        self.assertIsNotNone(user)
        self.assertNotEqual(user.loans_root_hash, ZERO32)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        loan = TreasuryLoanRecord.from_storage(self.node, loan_head)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.payment_amount, payment_amount)
        self.assertEqual(loan.loan_type, LoanType.SECURED)
        self.assertEqual(loan.next_payment_block_number, self.block.height + INTERVAL)
        self.assertEqual(
            loan.creation_block_number + loan.payment_interval_blocks * loan.payment_count,
            self.block.height + INTERVAL * COUNT,
        )

    # --- failures ---

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_BORROW,
            payment_interval_blocks=INTERVAL, payment_count=COUNT,
            loan_type=LoanType.SECURED, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_amount_zero_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        record = TreasuryUserRecord(balance=10_000, loans_root_hash=ZERO32, total_interest_paid=0)
        seed_treasury_account(
            self.node, self.block, treasury_balance=10_000,
            user_records={sender_pk: record},
        )
        with self.assertRaises(ValueError):
            self._make_borrow_tx(sender_pk, sender_key, amount=0)

    def test_no_user_record_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        seed_treasury_account(self.node, self.block, treasury_balance=10_000)
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=100)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_collateral_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        scheduled_total = payment_amount * COUNT
        # collateral < scheduled_total
        record = TreasuryUserRecord(
            balance=100, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=10_000,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=payment_amount)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_treasury_insufficient_balance_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        payment_amount = 100
        scheduled_total = payment_amount * COUNT
        rate_frac = windowed_rate_fraction(self.block, DURATION)
        discounted = calculate_discounted_amount(
            payment_amount=payment_amount, payment_interval_blocks=INTERVAL,
            payment_count=COUNT, rate_numerator=rate_frac[0], rate_denominator=rate_frac[1],
        )
        record = TreasuryUserRecord(
            balance=scheduled_total + 1000, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        # treasury balance < discounted
        seed_treasury_account(
            self.node, self.block, treasury_balance=1,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=payment_amount)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
