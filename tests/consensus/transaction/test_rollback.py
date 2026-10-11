"""Tests for the rollback of a failed top-level transaction."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = ROOT / "tests" / "consensus" / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.account import create_account
from astreum.consensus.constants import STORAGE_ADDRESS, TREASURY_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.transaction.fees import settle_fees
from astreum.consensus.transaction.apply import make_involved_variants
from astreum.consensus.transaction.treasury.record import LoanType, TreasuryLoanRecord
from astreum.expression import NIL
from astreum.machine.parser import parse
from astreum.machine.tokenizer import tokenize

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_program,
    seed_sender_account,
    seed_storage_account,
    seed_treasury_account,
    seed_user_with_loan,
    store_tx,
)


def _parse(program):
    expr, _ = parse(tokenize(program))
    return expr


class _Base(unittest.TestCase):

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()

    def _block(self, height=20):
        block = make_block(self.node, self.prev_block, height=height)
        seed_storage_account(block)
        self.inserted = []
        original = block.bloom_tree.insert

        def record(offset, elements, *a, **kw):
            self.inserted.extend(elements)
            return original(offset, elements, *a, **kw)

        block.bloom_tree.insert = record
        return block

    def _balance(self, block, address):
        account = block.accounts.get_account(address, self.node)
        return None if account is None else account.balance


class TestRollbackAfterHandler(_Base):
    """A handler writes. A late check then fails. The writes do not stay."""

    def _repay(self, balance, sender_pk=None, sender_key=None):
        """Apply a repay of a loan owned by a third party."""
        block = self._block()
        owner = self.owner
        block.accounts.set_account(owner, create_account(balance=0))
        block.accounts.set_account(sender_pk, create_account(balance=balance))
        loan_tx_id = os.urandom(32)
        loan = TreasuryLoanRecord(
            creation_block_number=0, loan_type=LoanType.SECURED,
            discounted_amount=450, payment_amount=100, payment_interval_blocks=10,
            next_payment_block_number=10, payment_count=5, owner=owner,
        )
        treasury = seed_treasury_account(self.node, block, treasury_balance=100_000)
        seed_user_with_loan(
            self.node, treasury, sender=sender_pk, user_balance=10_000,
            loan_tx_id=loan_tx_id, loan=loan,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=100, code=TransactionCode.TREASURY_REPAY,
            loan_transaction_id=loan_tx_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        apply_transaction(self.node, block, tx_hash)
        flush_pending(self.node, block)
        return block, tx_hash, block.receipts[-1]

    def setUp(self):
        super().setUp()
        self.owner = os.urandom(32)
        self.sender_pk, self.sender_key = seed_sender_account(
            self._block(), balance=1
        )

    def test_late_failure_rolls_back_handler(self):
        block, _, receipt = self._repay(
            10_000_000, self.sender_pk, self.sender_key
        )
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertGreater(self._balance(block, self.owner), 0)
        needed = receipt.total_fee + 100

        block, tx_hash, receipt = self._repay(
            needed - 1, self.sender_pk, self.sender_key
        )
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(self._balance(block, self.owner), 0)
        self.assertEqual(self._balance(block, TREASURY_ADDRESS), 100_000)
        self.assertEqual(
            self._balance(block, self.sender_pk), needed - 1 - receipt.total_fee
        )
        self.assertEqual(block.involved_accounts, set())
        self.assertNotIn(
            make_involved_variants(tx_hash, self.owner)[1], self.inserted
        )

    def test_failed_tx_keeps_search_elements(self):
        block, tx_hash, receipt = self._repay(
            10_000_000, self.sender_pk, self.sender_key
        )
        needed = receipt.total_fee + 100
        self.inserted.clear()
        block, tx_hash, receipt = self._repay(
            needed - 1, self.sender_pk, self.sender_key
        )
        self.assertEqual(receipt.status, STATUS_FAILED)
        # The hash, sender and receiver search elements are still inserted.
        self.assertEqual(len(self.inserted), 7)


class TestRollbackFailedTransfer(_Base):

    def _failed_transfer(self, balance=1_000_000, amount=2_000_000):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=balance)
        recipient = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=amount, code=TransactionCode.TRANSFER, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        return block, sender_pk, recipient, block.receipts[-1]

    def test_failed_tx_charges_fees_and_counter(self):
        block, sender_pk, recipient, receipt = self._failed_transfer()
        sender = block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(sender.counter, 1)
        self.assertEqual(
            sender.balance, 1_000_000 - receipt.transaction_fee - receipt.storage_fee
        )

    def test_failed_tx_does_not_create_recipient(self):
        block, _, recipient, receipt = self._failed_transfer()
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertIsNone(block.accounts.get_account(recipient, self.node))

    def test_counter_mismatch_not_consumed(self):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        tx = create_transaction(
            chain_id=1, counter=9, sender=sender_pk, recipient=os.urandom(32),
            amount=10, code=TransactionCode.TRANSFER, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        self.assertEqual(block.receipts[-1].status, STATUS_FAILED)
        self.assertEqual(block.accounts.get_account(sender_pk, self.node).counter, 0)

    def test_successful_tx_unchanged(self):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=1_000_000)
        recipient = os.urandom(32)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=500, code=TransactionCode.TRANSFER, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        self.assertEqual(block.receipts[-1].status, STATUS_SUCCESS)
        self.assertEqual(self._balance(block, recipient), 500)
        self.assertEqual(block.accounts.get_account(sender_pk, self.node).counter, 1)


class TestRollbackCall(_Base):

    COST_LIMIT = 400

    def _call(self, balance):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=balance)
        third = os.urandom(32)
        block.accounts.set_account(
            third,
            create_account(
                balance=0, code_hash=seed_program(self.node, _parse("(1 drop)"))
            ),
        )
        program = _parse(f"(7 tx.log 65 0x{third.hex()} 0 '() tx.new drop)")
        contract = os.urandom(32)
        block.accounts.set_account(
            contract,
            create_account(balance=100_000, code_hash=seed_program(self.node, program)),
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=contract,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=self.COST_LIMIT,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        return block, contract, third, block.receipts[-1]

    def test_late_failure_rolls_back_call_and_keeps_logs(self):
        block, contract, _, receipt = self._call(10_000_000)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(block.accounts.get_account(contract, self.node).counter, 1)

        self.inserted.clear()
        block, contract, third, receipt = self._call(receipt.total_fee - 1)
        self.assertEqual(receipt.status, STATUS_FAILED)
        # The nested call and its counter increment do not stay.
        self.assertEqual(block.accounts.get_account(contract, self.node).counter, 0)
        self.assertEqual(block.involved_accounts, set())
        # The logs stay in the block.
        pending = {expr.hash() for expr in block.pending_exprs}
        self.assertIn(receipt.logs_hash, pending)



class TestLogBudget(_Base):
    """A log that does not fit is dropped. The logs before it stay."""

    def _run(self, logs, balance, nested=False):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=balance)
        third = os.urandom(32)
        block.accounts.set_account(
            third,
            create_account(
                balance=0, code_hash=seed_program(self.node, _parse("(1 drop)"))
            ),
        )
        calls = f"65 0x{third.hex()} 0 '() tx.new drop " if nested else ""
        puts = " ".join(f"{i + 1} tx.log" for i in range(logs))
        contract = os.urandom(32)
        block.accounts.set_account(
            contract,
            create_account(
                balance=0,
                code_hash=seed_program(self.node, _parse(f"({calls}{puts})")),
            ),
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=contract,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=200,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        flush_pending(self.node, block)
        sender = block.accounts.get_account(sender_pk, self.node)
        contract_account = block.accounts.get_account(contract, self.node)
        return block, block.receipts[-1], sender, contract_account

    def test_last_log_dropped_earlier_logs_kept_and_paid(self):
        _, two, _, _ = self._run(2, 10_000_000, nested=True)
        # The balance covers 2 logs and the 3rd log does not fit.
        block, receipt, sender, contract = self._run(3, 300, nested=True)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(receipt.logs_hash, two.logs_hash)
        self.assertIn(receipt.logs_hash, {e.hash() for e in block.pending_exprs})
        # The sender pays what it has.
        self.assertEqual(sender.balance, 0)
        self.assertEqual(receipt.total_fee, 300)
        # The state of the call is rolled back.
        self.assertEqual(contract.counter, 0)
        self.assertEqual(block.involved_accounts, set())

    def test_first_log_does_not_fit(self):
        _, none, _, _ = self._run(0, 10_000_000, nested=True)
        # 201 passes the cost_limit guard (1 + 200) and covers no log.
        block, receipt, sender, _ = self._run(3, 201, nested=True)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(receipt.logs_hash, none.logs_hash)
        self.assertEqual(sender.balance, 0)

    def test_logs_that_fit_succeed(self):
        _, three, _, _ = self._run(3, 10_000_000, nested=True)
        _, receipt, sender, _ = self._run(3, three.total_fee, nested=True)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(receipt.logs_hash, three.logs_hash)
        self.assertEqual(sender.balance, 0)


class TestPartialFees(_Base):
    """A failed transaction pays min(balance, fees)."""

    def _transfer(self, balance, amount):
        block = self._block()
        sender_pk, sender_key = seed_sender_account(block, balance=balance)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=amount, code=TransactionCode.TRANSFER, secret_key=sender_key,
        )
        apply_transaction(self.node, block, store_tx(self.node, tx))
        sender = block.accounts.get_account(sender_pk, self.node)
        return block, block.receipts[-1], sender

    def test_balance_below_fees_pays_whole_balance(self):
        _, full, _ = self._transfer(10_000_000, 0)
        balance = full.total_fee - 5
        block, receipt, sender = self._transfer(balance, 0)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(sender.balance, 0)
        self.assertEqual(receipt.total_fee, balance)
        self.assertEqual(self._balance(block, STORAGE_ADDRESS), receipt.storage_fee)

    def test_balance_below_base_fee_pays_balance_to_base_fee(self):
        _, full, _ = self._transfer(10_000_000, 0)
        block, receipt, sender = self._transfer(1, 0)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(sender.balance, 0)
        self.assertEqual(receipt.transaction_fee, 1)
        self.assertEqual(receipt.storage_fee, 0)

    def test_amount_unaffordable_pays_fees_only(self):
        _, full, _ = self._transfer(10_000_000, 0)
        balance = full.total_fee + 100
        block, receipt, sender = self._transfer(balance, 1_000_000)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(sender.balance, balance - receipt.total_fee)
        # No new-account fee: the failed transaction creates no recipient.
        self.assertLess(receipt.total_fee, full.total_fee)


class TestSettleFees(unittest.TestCase):

    def test_all_paid(self):
        s = settle_fees(100, 1, 2, 3, 10)
        self.assertEqual((s.transaction_fee, s.data_fee, s.storage_fee, s.transfer_amount), (1, 2, 3, 10))
        self.assertTrue(s.affordable)

    def test_amount_does_not_fit(self):
        s = settle_fees(15, 1, 2, 3, 10)
        self.assertEqual((s.transaction_fee, s.data_fee, s.storage_fee, s.transfer_amount), (1, 2, 3, 0))
        self.assertFalse(s.affordable)

    def test_base_fee_first(self):
        s = settle_fees(4, 3, 2, 10, 0)
        self.assertEqual((s.transaction_fee, s.data_fee, s.storage_fee), (3, 1, 0))
        self.assertEqual(s.total, 4)

    def test_balance_below_one_fee(self):
        s = settle_fees(1000, 1_000_000, 0, 5, 0)
        self.assertEqual((s.transaction_fee, s.storage_fee), (1000, 0))

    def test_zero_balance(self):
        self.assertEqual(settle_fees(0, 1, 1, 1, 0).total, 0)


if __name__ == "__main__":
    unittest.main()
