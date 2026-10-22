"""Validation tests for the CHANNEL_UPDATE (0x10) transaction code.

Prerequisites: an existing channel from sender to counterparty; tx recipient
must equal sender (self-update). Payload is counterparty(32) optionally
followed by a new withdrawal_window(8).
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
    FAR_FUTURE_WINDOW,
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_channel,
    seed_sender_account,
    seed_storage_account,
    store_tx,
)


class TestChannelUpdate(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block(timestamp=0)
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def _get_channel(self, account, counterparty):
        head = get_from_radix_tree(account.channels, self.node, counterparty)
        return Channel.from_storage(self.node, head.hash())

    # --- success ---

    def test_update_adds_balance_and_increments_counter(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=500, counter=3, withdrawal_window=FAR_FUTURE_WINDOW,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=200, code=TransactionCode.CHANNEL_UPDATE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        ch = self._get_channel(sender, counterparty)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(ch.balance, 700)
        self.assertEqual(ch.counter, 4)
        self.assertEqual(ch.withdrawal_window, FAR_FUTURE_WINDOW)
    
    def test_update_extends_withdrawal_window(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        current_window = 1000
        new_window = current_window + 1000
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=100, counter=1, withdrawal_window=current_window,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=0, code=TransactionCode.CHANNEL_UPDATE,
            counterparty=counterparty, new_withdrawal_window=new_window,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        ch = self._get_channel(sender, counterparty)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(ch.withdrawal_window, new_window)

    # --- failures ---

    def test_recipient_not_self_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=500, counter=3, withdrawal_window=FAR_FUTURE_WINDOW,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            amount=200, code=TransactionCode.CHANNEL_UPDATE,
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
            amount=200, code=TransactionCode.CHANNEL_UPDATE,
            counterparty=counterparty, secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_shortening_window_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=1_000_000)
        sender_acct = self.block.accounts.get_account(sender_pk, self.node)
        counterparty = os.urandom(32)
        current_window = 5000
        seed_channel(
            self.node, sender_acct, counterparty,
            balance=100, counter=1, withdrawal_window=current_window,
        )
        self.block.accounts.set_account(sender_pk, sender_acct)

        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=sender_pk,
            amount=0, code=TransactionCode.CHANNEL_UPDATE,
            counterparty=counterparty, new_withdrawal_window=1000,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
