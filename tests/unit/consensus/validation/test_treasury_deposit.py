"""Validation tests for the TREASURY_DEPOSIT (0x20) transaction code.

A deposit credits the treasury's balance and increments the sender's staking
TreasuryUserRecord. The recipient must be the TREASURY_ADDRESS and the sender
must already have an existing TreasuryUserRecord in the treasury's stake trie.
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
from astreum.consensus.transaction.treasury.record import TreasuryUserRecord
from astreum.expression import ZERO32
from astreum.storage.radix import get_from_radix_tree
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


class TestTreasuryDeposit(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    # --- success ---

    def test_deposit_increases_stake_and_treasury_balance(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        record = TreasuryUserRecord(
            balance=1_000, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=10_000,
            user_records={sender_pk: record},
        )

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=500, code=TransactionCode.TREASURY_DEPOSIT,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        # Treasury balance increased by the deposit amount.
        self.assertEqual(treasury.balance, 10_500)   # 10_000 + 500
        # The stake trie root changed (record was updated).
        self.assertNotEqual(treasury.data_hash, ZERO32)

    # --- failures ---

    def test_recipient_not_treasury_does_not_credit_record(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        record = TreasuryUserRecord(
            balance=1_000, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=10_000,
            user_records={sender_pk: record},
        )
        other = os.urandom(32)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=other,
            amount=500, code=TransactionCode.TREASURY_DEPOSIT,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        record_after = TreasuryUserRecord.from_storage(
            self.node, get_from_radix_tree(treasury.data, self.node, sender_pk).hash(),
        )
        # recipient != TREASURY → transfer_amount forced to 0; record untouched
        self.assertEqual(record_after.balance, 1_000)
        self.assertEqual(treasury.balance, 10_000)

    def test_no_existing_user_record_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        # treasury exists but has NO record for the sender
        seed_treasury_account(
            self.node, self.block, treasury_balance=10_000,
        )

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=500, code=TransactionCode.TREASURY_DEPOSIT,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_insufficient_balance_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1)
        record = TreasuryUserRecord(
            balance=1_000, loans_root_hash=ZERO32, total_interest_paid=0,
        )
        seed_treasury_account(
            self.node, self.block,
            treasury_balance=10_000,
            user_records={sender_pk: record},
        )

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            amount=500, code=TransactionCode.TREASURY_DEPOSIT,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
