"""Validation tests for the unsecured TREASURY_BORROW (0x21) path.

An unsecured perpetual borrow needs no guarantee and no collateral: the
borrower takes any principal ``L = transaction.amount``, the treasury mints
it, and the per-interval insurance premium is the whole cost. ``L`` is
credited to the borrower and the treasury is not debited, so the supply
level grows by the mint. The premium is recorded at origination as
``insurance_interval_fee`` and collected with the service, never deducted
from the principal. The locked payment is ``ceil(L * r_bar)``.
"""

import inspect
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

from astreum.consensus.models.statistics import BlockStatistics
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.treasury.record import (
    LoanType,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.storage.radix import RadixTree, get_from_radix_tree

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

# r_bar = 250_000 / 1_000_000 = 1/4, so payment = ceil(L / 4).
STAKE = 1_000_000
CTF = 250_000


def expected_payment(principal: int, fee: int = CTF, stake: int = STAKE) -> int:
    """Return the plan's per-block payment for *principal* at this rate."""
    return (principal * fee + stake - 1) // stake


def expected_insurance_interval_fee(
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
            cumulative_fee=CTF,
            )
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)

    def _make_borrow_tx(self, sender_pk, sender_key, *, amount, counter=0):
        return create_transaction(
            chain_id=1, counter=counter, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=amount, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.UNSECURED, secret_key=sender_key,
        )

    def _read_loan(self, sender_pk, tx_hash):
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        borrower_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        borrower = TreasuryUserRecord.from_storage(self.node, borrower_head)
        self.assertIsNotNone(borrower)
        loans_trie = RadixTree(root_hash=bytes(borrower.loans_root_hash))
        loan_head = get_from_radix_tree(loans_trie, self.node, tx_hash)
        return borrower, TreasuryLoanRecord.from_storage(self.node, loan_head)

    def _borrow(self, *, principal, treasury_balance):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        seed_treasury_account(
            self.node, self.block, treasury_balance=treasury_balance,
        )
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=principal)
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        supply_before = self.block.total_balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        return sender_pk, tx_hash, sender_before, supply_before

    # --- the mint ---

    def test_an_unsecured_borrow_takes_any_principal_with_only_the_premium(self):
        principal = 100
        sender_pk, tx_hash, sender_before, _ = self._borrow(
            principal=principal, treasury_balance=0,
        )

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)

        # No price, no fee haircut: the borrower receives the whole principal.
        self.assertEqual(sender.balance, sender_before + principal - receipt.total_fee)
        # The treasury mints the principal; it is not debited and is not a gate.
        self.assertEqual(treasury.balance, 0)

        borrower, loan = self._read_loan(sender_pk, tx_hash)
        self.assertIsNotNone(loan)
        self.assertEqual(borrower.loaned, principal)
        self.assertEqual(loan.loan_type, LoanType.UNSECURED)
        self.assertEqual(loan.discounted_amount, principal)
        self.assertEqual(loan.payment_amount, expected_payment(principal))
        self.assertEqual(loan.paid_until_block, self.block.height)
        self.assertEqual(loan.insurance_interval_fee, 0)  # first-ever unsecured loan
        self.assertEqual(loan.missed_count, 0)
        self.assertEqual(loan.owner, TREASURY_ADDRESS)
        self.assertEqual(self.block.statistics.total_loaned, principal)
        self.assertEqual(self.block.statistics.total_loan_count, 1)

    def test_a_treasury_balance_below_the_principal_does_not_gate_the_borrow(self):
        principal = 100
        sender_pk, _, sender_before, _ = self._borrow(
            principal=principal, treasury_balance=principal - 1,
        )

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        sender = self.block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance, sender_before + principal - self.block.receipts[-1].total_fee,
        )
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.balance, principal - 1)

    def test_the_borrow_mints_the_principal_into_the_supply(self):
        principal = 100
        _, _, _, supply_before = self._borrow(principal=principal, treasury_balance=0)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        # The mint is the credit of L; the burned fees (everything but the
        # storage fee, which is re-credited) are a separate reduction.
        burned = receipt.total_fee - receipt.storage_fee
        self.assertEqual(self.block.total_balance - supply_before + burned, principal)
        self.assertGreater(self.block.total_balance, supply_before)

    def test_the_principal_is_unbounded_by_the_treasury_balance(self):
        principal = 1_000_000_000
        sender_pk, tx_hash, _, _ = self._borrow(
            principal=principal, treasury_balance=0,
        )

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(treasury.balance, 0)
        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertEqual(loan.discounted_amount, principal)

    # --- the premium is the whole cost ---

    def test_the_premium_is_the_measured_interval_fee_not_a_haircut(self):
        principal = 100
        self.block.statistics = BlockStatistics(total_loan_count=10, total_loaned=10_000, total_written_off=3_333)

        fee = expected_insurance_interval_fee(
            principal=principal, global_loan_count=10,
            global_loaned=10_000, global_defaulted=3_333,
        )
        self.assertEqual(fee, 34)  # ceil(33.33), not floor(33.33)

        sender_pk, tx_hash, sender_before, _ = self._borrow(
            principal=principal, treasury_balance=0,
        )

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertEqual(loan.insurance_interval_fee, fee)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(
            sender.balance,
            sender_before + principal - receipt.total_fee,
        )

    def test_a_premium_at_or_above_the_principal_fails(self):
        # Every loaned unit defaulted: p = 1, so I = L.
        self.block.statistics = BlockStatistics(total_loan_count=10, total_loaned=10_000, total_written_off=10_000)

        self._borrow(principal=100, treasury_balance=0)

        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    # --- the guarantee apparatus is not reachable from a borrow ---

    def test_create_transaction_takes_no_guarantee_parameter(self):
        parameters = inspect.signature(create_transaction).parameters
        for name in ("guarantee_amount", "guarantee_refs", "price", "expiry"):
            self.assertNotIn(name, parameters)

    def test_a_borrow_transaction_carries_only_the_loan_type(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = self._make_borrow_tx(sender_pk, sender_key, amount=100)

        from astreum.consensus.transaction.treasury.utils import _data_nodes

        nodes = _data_nodes(tx.data)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].value, int(LoanType.UNSECURED))

    def test_the_loan_record_carries_no_guarantee_field(self):
        sender_pk, tx_hash, _, _ = self._borrow(principal=100, treasury_balance=0)

        _, loan = self._read_loan(sender_pk, tx_hash)
        self.assertFalse(hasattr(loan, "claimed_guarantees"))

    # --- failures ---

    def test_recipient_not_treasury_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=100, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.UNSECURED, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_zero_principal_is_rejected_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        with self.assertRaises(ValueError):
            self._make_borrow_tx(sender_pk, sender_key, amount=0)

    def test_a_claim_cost_borrow_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000_000)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_BORROW,
            loan_type=LoanType.CLAIM_COST, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
