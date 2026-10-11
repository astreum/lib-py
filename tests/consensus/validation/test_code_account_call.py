"""Validation tests for the CODE_ACCOUNT_CALL (0x41) transaction code."""

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

from astreum.consensus.account import create_account
from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.code import TransactionCode
from astreum.expression import Expr, NIL, ZERO32, bytes_
from astreum.machine.parser import parse
from astreum.machine.tokenizer import tokenize
from astreum.storage.radix import get_from_radix_tree, put_in_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_expr_list,
    seed_program,
    seed_sender_account,
    seed_storage_account,
    store_tx,
)
from astreum.expression.helpers import get_expr_builtin_tag


def _parse(program):
    expr, _ = parse(tokenize(program))
    return expr


class TestCodeAccountCall(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def _seed_expression_account(self, program, *, balance=0):
        code_hash = seed_program(self.node, program)
        recipient = os.urandom(32)
        acct = create_account(balance=balance, code_hash=code_hash)
        self.block.accounts.set_account(recipient, acct)
        return recipient, acct

    # --- success ---

    def test_acc_balance_reads_balance(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        prog = _parse("(drop acc.balance)")
        recipient, _ = self._seed_expression_account(prog, balance=5000)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=1000, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertGreater(receipt.transaction_fee, 0)

        acct = self.block.accounts.get_account(recipient, self.node)
        self.assertEqual(acct.balance, 6000)

    def test_execution_is_charged_once_and_counted_once(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        prog = _parse("(drop acc.balance)")
        recipient, _ = self._seed_expression_account(prog, balance=0)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=1000, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        before = self.block.accounts.get_account(sender_pk, self.node).balance
        apply_transaction(self.node, self.block, store_tx(self.node, tx))
        receipt = self.block.receipts[-1]
        self.assertGreater(receipt.execution_fee, 0)

        # The execution is inside transaction_fee: the sender pays it once,
        # and the block's fee totals (transaction + storage) hold it once.
        after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertEqual(before - after, 1000 + receipt.total_fee)
        self.assertEqual(
            receipt.total_fee, receipt.transaction_fee + receipt.storage_fee + receipt.data_fee
        )
        self.assertGreaterEqual(receipt.transaction_fee, 1 + receipt.execution_fee)

    def test_tx_new_transfers_to_sender(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        prog = _parse("(0 tx.sender 1000 '() tx.new drop)")
        recipient, _ = self._seed_expression_account(prog, balance=5000)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=1000, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=500,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender = self.block.accounts.get_account(sender_pk, self.node)
        recipient_acct = self.block.accounts.get_account(recipient, self.node)
        self.assertEqual(recipient_acct.balance, 5000)
        self.assertGreater(sender.balance, 990_000)

    def test_tx_new_to_new_account_creates_recipient(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=2_000_000)
        new_addr = os.urandom(32)
        prog = _parse("(0 0x" + new_addr.hex() + " 1000 '() tx.new drop)")
        recipient, _ = self._seed_expression_account(prog, balance=5000)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=1500,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        new_acct = self.block.accounts.get_account(new_addr, self.node)
        self.assertIsNotNone(new_acct)
        self.assertEqual(new_acct.balance, 1000)

        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        self.assertGreater(storage_account.balance, 0)

    def test_acc_get_reads_from_data_trie(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        key = b"\xde\xad\xbe\xef"
        value = b"\xca\xfe\xba\xbe"
        prog = _parse("(drop 0xdeadbeef acc.get)")
        recipient, acct = self._seed_expression_account(prog, balance=0)
        put_in_radix_tree(acct.data, self.node, key, value)
        acct.data_hash = acct.data.root_hash or ZERO32
        self.block.accounts.set_account(recipient, acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

    def test_acc_put_writes_to_data_trie(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=2_000_000)
        prog = _parse("(drop 0xdeadbeef 0xcafebabe acc.put)")
        recipient, _ = self._seed_expression_account(prog, balance=0)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=500,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        acct = self.block.accounts.get_account(recipient, self.node)
        stored = get_from_radix_tree(acct.data, self.node, b"\xde\xad\xbe\xef")
        self.assertEqual(get_expr_builtin_tag(stored), "bytes")
        self.assertEqual(stored.value, b"\xca\xfe\xba\xbe")

        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        self.assertGreater(storage_account.balance, 0)

    # --- failures ---

    def test_tx_new_insufficient_balance_soft_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        prog = _parse("(0 tx.sender 2147483647 '() tx.new drop)")
        recipient, _ = self._seed_expression_account(prog, balance=1000)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=500,
        )
        tx_hash = store_tx(self.node, tx)
        sender_before = self.block.accounts.get_account(sender_pk, self.node).balance
        recipient_before = self.block.accounts.get_account(recipient, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        # Soft-fail: tx.new pushes NIL on insufficient balance;
        # outer contract call continues successfully.
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        sender_after = self.block.accounts.get_account(sender_pk, self.node).balance
        self.assertLess(sender_after, sender_before)  # sender paid outer fees
        recipient_after = self.block.accounts.get_account(recipient, self.node).balance
        # No transfer happened — contract could not afford it.
        self.assertEqual(recipient_after, recipient_before)

    def test_call_missing_recipient_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        missing = os.urandom(32)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=missing,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_call_zero_code_hash_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        recipient = os.urandom(32)
        acct = create_account(balance=0, code_hash=ZERO32)
        self.block.accounts.set_account(recipient, acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)

    def test_call_program_not_in_storage_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        recipient = os.urandom(32)
        fake_code_hash = os.urandom(32)
        acct = create_account(balance=0, code_hash=fake_code_hash)
        self.block.accounts.set_account(recipient, acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=recipient,
            amount=0, code=TransactionCode.CODE_ACCOUNT_CALL,
            data=NIL, secret_key=sender_key, cost_limit=100,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_FAILED)



if __name__ == "__main__":
    unittest.main()
