"""Validation tests for the CODE_ACCOUNT_CREATE (0x40) transaction code.

A code-account-create deploys a new expression account whose address is the
create transaction's hash. The tx recipient must be empty, data must be a
32-byte program hash, and the tx amount becomes the new account's initial
balance.
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
from astreum.expression import Expr, symbol
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_program,
    seed_storage_account,
    seed_sender_account,
    store_tx,
)


def _sample_program() -> Expr:
    """A trivial valid program expr: a Symbol (no ListExpr in this codebase)."""
    return symbol("hello")


class TestCodeAccountCreate(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    # --- success ---

    def test_create_deploys_expression_account(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        program_hash = seed_program(self.node, _sample_program())
        initial_balance = 5_000

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=b"",
            amount=initial_balance, code=TransactionCode.CODE_ACCOUNT_CREATE,
            program_hash=program_hash, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        expr_account = self.block.accounts.get_account(tx_hash, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertIsNotNone(expr_account)
        self.assertEqual(expr_account.balance, initial_balance)
        self.assertEqual(expr_account.code_hash, program_hash)
        self.assertEqual(
            sender.balance,
            sender_before - initial_balance - receipt.transaction_fee - receipt.storage_fee,
        )

    # --- failures ---

    def test_recipient_not_empty_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        program_hash = seed_program(self.node, _sample_program())

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=5_000, code=TransactionCode.CODE_ACCOUNT_CREATE,
            program_hash=program_hash, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_account_already_exists_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        program_hash = seed_program(self.node, _sample_program())

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=b"",
            amount=5_000, code=TransactionCode.CODE_ACCOUNT_CREATE,
            program_hash=program_hash, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        # Pre-seed an account at the tx_hash address.
        from astreum.consensus.account import create_account
        self.block.accounts.set_account(tx_hash, create_account(balance=999))

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_balance_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1)
        program_hash = seed_program(self.node, _sample_program())

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=b"",
            amount=5_000, code=TransactionCode.CODE_ACCOUNT_CREATE,
            program_hash=program_hash, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
