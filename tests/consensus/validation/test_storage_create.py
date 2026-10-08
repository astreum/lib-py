"""Validation tests for the STORAGE_CREATE (0x30) transaction code.

A storage-create pays for the exprs in a list. It writes no record and no trie
entry: the block record covers the items, which arrive with the transaction
tree. The recipient must be the STORAGE_ADDRESS and amount must be 0 (amount > 0
is rejected).
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
from astreum.consensus.block.rate import calculate_storage_fee
from astreum.consensus.transaction.storage.initial import storage_walk_size
from astreum.expression import Expr, bytes_
from astreum.storage.radix import get_from_radix_tree
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_expr_list,
    seed_sender_account,
    seed_storage_account,
    store_tx,
)


class TestStorageCreate(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    # --- success ---

    def test_create_charges_the_items_and_writes_no_record(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        exprs = [bytes_(b"atom-alpha"), bytes_(b"atom-beta")]
        list_id = seed_expr_list(self.node, exprs)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=STORAGE_ADDRESS,
            amount=0, code=TransactionCode.STORAGE_CREATE,
            expr_list_id=list_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)
        storage_before = self.block.accounts.get_account(STORAGE_ADDRESS, self.node).balance

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        # No record and no trie entry for the list.
        self.assertIsNone(get_from_radix_tree(storage_account.data, self.node, list_id))
        self.assertIsNone(storage_account.data.root_hash)

        # The items are charged at the record walk's size, on top of the
        # receipt's storage fee, all paid to the storage account.
        item_bytes = sum(storage_walk_size(self.node, self.block, e) for e in exprs)
        items_fee = calculate_storage_fee(self.block, item_bytes)
        self.assertEqual(
            storage_account.balance - storage_before,
            items_fee + receipt.storage_fee,
        )

    # --- failures ---

    def test_recipient_not_burn_creates_no_record(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        exprs = [bytes_(b"atom-a")]
        list_id = seed_expr_list(self.node, exprs)
        other = os.urandom(32)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=other,
            amount=0, code=TransactionCode.STORAGE_CREATE,
            expr_list_id=list_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        # recipient != STORAGE_ADDRESS → no record created under storage data
        self.assertIsNone(get_from_radix_tree(storage_account.data, self.node, list_id))

    def test_amount_to_burn_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        exprs = [bytes_(b"atom-a")]
        list_id = seed_expr_list(self.node, exprs)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=STORAGE_ADDRESS,
            amount=1000, code=TransactionCode.STORAGE_CREATE,
            expr_list_id=list_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_same_list_twice_is_charged_twice(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        exprs = [bytes_(b"atom-a")]
        list_id = seed_expr_list(self.node, exprs)

        balances = []
        for counter in (0, 1):
            tx = create_transaction(
                chain_id=1, counter=counter, sender=sender_pk, recipient=STORAGE_ADDRESS,
                amount=0, code=TransactionCode.STORAGE_CREATE,
                expr_list_id=list_id, secret_key=sender_key,
            )
            before = self.block.accounts.get_account(STORAGE_ADDRESS, self.node).balance
            apply_transaction(self.node, self.block, store_tx(self.node, tx))
            self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
            balances.append(
                self.block.accounts.get_account(STORAGE_ADDRESS, self.node).balance - before
            )
        self.assertEqual(balances[0], balances[1])
        self.assertGreater(balances[0], 0)

    def test_list_id_not_found_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        missing_id = os.urandom(32)  # not stored in node

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=STORAGE_ADDRESS,
            amount=0, code=TransactionCode.STORAGE_CREATE,
            expr_list_id=missing_id, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
