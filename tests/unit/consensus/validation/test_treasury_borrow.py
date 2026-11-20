"""Validation tests for the secured TREASURY_BORROW (0x21) transaction code.

A perpetual borrow lends the principal ``L = transaction.amount`` and locks a
per-block payment at the all-time rate ``r_bar = total_fees /
cumulative_total_balance`` read from the previous block:

    payment_amount = (L * fee + stake - 1) // stake = ceil(L * r_bar)

There is no interval and no payment count. ``paid_until_block`` starts at the
borrow's own block height.
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
from astreum.consensus.transaction.treasury.borrow import (
    _per_block_payment,
    handle_treasury_borrow,
)
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.expression import NIL, resolve_list_exprs
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
    seed_user_with_loan,
    store_tx,
)

# r_bar = 250_000 / 1_000_000 = 1/4, so payment = ceil(L / 4).
STAKE = 1_000_000
CTF = 250_000


def expected_payment(principal: int, fee: int = CTF, stake: int = STAKE) -> int:
    """Return the plan's per-block payment for *principal* at this rate."""
    return (principal * fee + stake - 1) // stake


class TestTreasuryBorrow(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(
            cumulative_total_balance=STAKE, total_balance=STAKE,
            cumulative_fee=CTF, total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount, counter=0):
        return create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.SECURED, secret_key=sender_key,
        )

    def _read_loan(self, sender_pk, tx_hash):
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        self.assertIsNotNone(user)
        self.assertNotEqual(user.loans_root_hash, ZERO32)
        loans_trie = RadixTree(root_hash=bytes(user.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        return user, TreasuryLoanRecord.from_storage(self.node, loan_head)

    # --- success ---

    def test_borrow_locks_the_per_block_payment_at_the_all_time_rate(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 101
        self.assertEqual(expected_payment(principal), 26)  # ceil(25.25)

        record = TreasuryUserRecord(
            balance=principal + 1000,
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=principal + 1000,
            user_records={sender_pk: record},
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        treasury_before = self.block.accounts.get_account(TREASURY_ADDRESS, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        # The treasury lends exactly the principal.
        self.assertEqual(
            sender.balance,
            sender_before + principal - receipt.total_fee,
        )
        self.assertEqual(treasury.balance, treasury_before - principal)

        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertIsNotNone(loan)
        self.assertEqual(loan.loan_type, LoanType.SECURED)
        self.assertEqual(loan.discounted_amount, principal)
        self.assertEqual(loan.payment_amount, expected_payment(principal))
        self.assertEqual(loan.owner, TREASURY_ADDRESS)

    def test_borrow_payment_rounds_up(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 1
        record = TreasuryUserRecord(
            balance=principal,
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=principal,
            user_records={sender_pk: record},
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        _, loan = self._read_loan(sender_pk, tx_hash)
        # r_bar = 1/4, so ceil(1/4) rounds up to 1, not down to 0.
        self.assertEqual(loan.payment_amount, 1)

    def test_paid_until_block_is_the_origination_height(self):
        self.block = make_block(self.node, self.prev_block, height=7)
        seed_storage_account(self.block)
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        record = TreasuryUserRecord(
            balance=principal,
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=principal,
            user_records={sender_pk: record},
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertEqual(loan.creation_block_number, 7)
        self.assertEqual(loan.paid_until_block, 7)

    def test_closed_secured_loan_does_not_reduce_available_collateral(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk,
            user_balance=principal,  # no capacity consumed by the closed loan
            loan_tx_id=os.urandom(32),
            loan=TreasuryLoanRecord(
                creation_block_number=0,
                loan_type=LoanType.SECURED,
                discounted_amount=500,
                payment_amount=125,
                paid_until_block=0,
            ),
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)

    def test_open_secured_loan_consumes_collateral(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk,
            user_balance=principal + 500,  # exactly 500 left for the new loan
            loan_tx_id=os.urandom(32),
            loan=TreasuryLoanRecord(
                creation_block_number=0,
                loan_type=LoanType.SECURED,
                discounted_amount=500,
                payment_amount=125,
                paid_until_block=1,
            ),
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)

    def test_open_secured_loan_can_leave_too_little_collateral(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk,
            # 599 without the open loan would cover L, but 600 - 500 = 99.
            user_balance=principal + 500 - 1,
            loan_tx_id=os.urandom(32),
            loan=TreasuryLoanRecord(
                creation_block_number=0,
                loan_type=LoanType.SECURED,
                discounted_amount=500,
                payment_amount=125,
                paid_until_block=1,
            ),
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_open_unsecured_loan_does_not_reduce_collateral(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        treasury = seed_treasury_account(
            self.node, self.block, treasury_balance=principal + 1000,
        )
        seed_user_with_loan(
            self.node, treasury,
            sender=sender_pk,
            # Same 599 that fails against an open secured loan.
            user_balance=principal + 500 - 1,
            loan_tx_id=os.urandom(32),
            loan=TreasuryLoanRecord(
                creation_block_number=0,
                loan_type=LoanType.UNSECURED,
                discounted_amount=500,
                payment_amount=125,
                paid_until_block=1,
            ),
        )

        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)

    # --- rate guards ---

    def test_per_block_payment_rejects_non_positive_rate_inputs(self):
        self.assertIsNone(_per_block_payment(principal=100, rate_fraction=(1, 0)))
        self.assertIsNone(_per_block_payment(principal=100, rate_fraction=(0, 1000)))
        self.assertEqual(
            _per_block_payment(principal=100, rate_fraction=(1, 1000)),
            1,
        )

    # --- create / wire shape ---

    def test_borrow_request_has_no_interval_or_count(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=100)

        nodes, missed = resolve_list_exprs(self.node, tx.data)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].value, int(LoanType.SECURED))
        self.assertIs(nodes[1], NIL)

        with self.assertRaises(TypeError):
            create_transaction(
                chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
                amount=100, code=TransactionCode.TREASURY_BORROW,
                payment_interval_blocks=4, payment_count=2,
                loan_type=LoanType.SECURED, secret_key=sender_key,
            )

    # --- failures ---

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.SECURED, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_amount_zero_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
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
        principal = 100
        record = TreasuryUserRecord(
            balance=principal - 1,
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=10_000,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_treasury_insufficient_balance_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        record = TreasuryUserRecord(
            balance=principal,
            loans_root_hash=ZERO32,
            total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=principal - 1,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_zero_cumulative_fee_fails(self):
        # The block fee engine rejects zero fees before apply reaches the
        # handler, so the plan's guard is checked at the handler boundary.
        prev_block = make_previous_block(
            cumulative_total_balance=STAKE, total_balance=STAKE,
            cumulative_fee=0, total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, prev_block, height=1)
        seed_storage_account(self.block)
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        record = TreasuryUserRecord(
            balance=principal, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=principal,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)

        status = handle_treasury_borrow(
            node=self.node,
            block=self.block,
            transaction=tx,
            transaction_hash=tx_hash,
            sender_account=self.block.accounts.get_account(sender_pk, self.node),
            treasury_account=self.block.accounts.get_account(TREASURY_ADDRESS, self.node),
        )
        self.assertEqual(status, STATUS_FAILED)

    def test_zero_cumulative_balance_fails(self):
        prev_block = make_previous_block(
            cumulative_total_balance=0, total_balance=0,
            cumulative_fee=CTF, total_tx_fees=1, total_tx_bytes=1_000_000,
        )
        self.block = make_block(self.node, prev_block, height=1)
        seed_storage_account(self.block)
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        principal = 100
        record = TreasuryUserRecord(
            balance=principal, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block, treasury_balance=principal,
            user_records={sender_pk: record},
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)

        status = handle_treasury_borrow(
            node=self.node,
            block=self.block,
            transaction=tx,
            transaction_hash=tx_hash,
            sender_account=self.block.accounts.get_account(sender_pk, self.node),
            treasury_account=self.block.accounts.get_account(TREASURY_ADDRESS, self.node),
        )
        self.assertEqual(status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
