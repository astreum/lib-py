"""Validation tests for the CHANNEL_CLOSE (0x12) transaction code.

A close refunds the channel balance to the sender and is only valid after the
withdrawal window has passed (previous block timestamp > window). The tx
recipient must equal sender (self-close).
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
from astreum.consensus.transaction.channel.model import Channel
from astreum.consensus.transaction.code import TransactionCode
from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.storage.radix import get_from_radix_tree

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_channel,
    seed_storage_account,
    seed_sender_account,
    store_tx,
)


class TestChannelClose(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(timestamp=10_000)
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def _get_channel(self, account, counterparty):
        head = get_from_radix_tree(account.channels, self.node, counterparty)
        return Channel.from_storage(self.node, head.hash())

    # --- success ---

    def test_close_after_window_refunds_balance(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        past_window = 1000  # < prev_block.timestamp 10_000
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=800, counter=2, withdrawal_window=past_window,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=0, code=TransactionCode.CHANNEL_CLOSE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        sender_before = sender_acct.balance
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        ch = self._get_channel(sender, counterparty)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(ch.balance, 0)
        self.assertEqual(ch.counter, 3)               # +1
        self.assertEqual(
            sender.balance,
            sender_before + 800 - receipt.transaction_fee - receipt.storage_fee,
        )

    # --- failures ---

    def test_recipient_not_self_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        past_window = 1000
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=800, counter=2, withdrawal_window=past_window,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=0, code=TransactionCode.CHANNEL_CLOSE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_window_still_open_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        future_window = 100_000  # > prev_block.timestamp
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=800, counter=2, withdrawal_window=future_window,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=0, code=TransactionCode.CHANNEL_CLOSE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_no_existing_channel_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        counterparty = os.urandom(32)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=0, code=TransactionCode.CHANNEL_CLOSE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

if __name__ == "__main__":
    unittest.main()
