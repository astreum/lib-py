"""Validation tests for the CHANNEL_WITHDRAW (0x11) transaction code.

In a withdraw, ``tx.sender`` is the withdrawer/recipient of funds and
``tx.recipient`` is the payer (the channel owner). The payer must sign a
withdraw message authorising the withdrawal; the channel must be open
(withdrawal_window > previous block timestamp) and the requested counter must
exceed the stored counter.
"""

import os
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.transaction import apply_transaction, create_transaction
from astreum.consensus.transaction.channel.model import Channel
from astreum.consensus.transaction.channel.withdraw import _withdraw_message
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


def _make_withdraw_signature(*, chain_id, payer_pk, withdrawer_pk, counter, amount, payer_key):
    msg = _withdraw_message(
        chain_id=chain_id, payer=payer_pk, recipient=withdrawer_pk,
        counter=counter, amount=amount,
    )
    return payer_key.sign(msg)


class TestChannelWithdraw(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        # previous block timestamp well inside the withdrawal window
        self.prev_block = make_previous_block(timestamp=100)
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def _setup_open_channel(self, *, channel_balance=1000, stored_counter=5):
        """Set up withdrawer (sender) + payer (recipient) with an open channel."""
        withdrawer_pk, withdrawer_key = seed_sender_account(self.block, balance=1_000_000)
        payer_key = Ed25519PrivateKey.generate()
        payer_pk = payer_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        payer_acct = self.block.accounts.get_account(payer_pk, self.node)
        if payer_acct is None:
            from astreum.consensus.account import create_account
            payer_acct = create_account(balance=1_000_000)
        else:
            payer_acct.balance = 1_000_000
        self.block.accounts.set_account(payer_pk, payer_acct)

        window = 100_000  # > prev_block.timestamp (100)
        seed_channel(
            self.node, payer_acct, counterparty=withdrawer_pk,
            balance=channel_balance, counter=stored_counter,
            withdrawal_window=window,
        )
        self.block.accounts.set_account(payer_pk, payer_acct)
        return withdrawer_pk, withdrawer_key, payer_pk, payer_key

    def _get_channel(self, account, counterparty):
        head = get_from_radix_tree(account.channels, self.node, counterparty)
        return Channel.from_storage(self.node, head.hash())

    # --- success ---

    def test_withdraw_pays_out_and_reduces_channel_balance(self):
        w_pk, w_key, p_pk, p_key = self._setup_open_channel(
            channel_balance=1000, stored_counter=5,
        )
        counter = 6
        amount = 400
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=p_pk, withdrawer_pk=w_pk,
            counter=counter, amount=amount, payer_key=p_key,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=p_pk, counterparty=w_pk, payer=p_pk,
            withdraw_counter=counter, withdraw_amount=amount,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        withdrawer_before = self.block.accounts.get_account(w_pk, self.node).balance
        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        payer = self.block.accounts.get_account(p_pk, self.node)
        withdrawer = self.block.accounts.get_account(w_pk, self.node)
        ch = self._get_channel(payer, w_pk)
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(ch.balance, 600)          # 1000 - 400
        self.assertEqual(ch.counter, 6)
        self.assertEqual(
            withdrawer.balance,
            withdrawer_before + 400 - receipt.transaction_fee - receipt.storage_fee,
        )

    # --- failures ---

    def test_bad_signature_fails(self):
        w_pk, w_key, p_pk, _ = self._setup_open_channel()
        # sign with withdrawer's key instead of payer's
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=p_pk, withdrawer_pk=w_pk,
            counter=6, amount=400, payer_key=w_key,  # wrong key
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=p_pk, counterparty=w_pk, payer=p_pk,
            withdraw_counter=6, withdraw_amount=400,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_counter_not_greater_than_stored_fails(self):
        w_pk, w_key, p_pk, p_key = self._setup_open_channel(stored_counter=5)
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=p_pk, withdrawer_pk=w_pk,
            counter=5, amount=400, payer_key=p_key,  # not > 5
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=p_pk, counterparty=w_pk, payer=p_pk,
            withdraw_counter=5, withdraw_amount=400,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_amount_exceeds_channel_balance_fails(self):
        w_pk, w_key, p_pk, p_key = self._setup_open_channel(channel_balance=1000)
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=p_pk, withdrawer_pk=w_pk,
            counter=6, amount=2000, payer_key=p_key,  # > 1000
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=p_pk, counterparty=w_pk, payer=p_pk,
            withdraw_counter=6, withdraw_amount=2000,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_expired_window_fails(self):
        w_pk, w_key, p_pk, p_key = self._setup_open_channel()
        # override previous block timestamp to exceed the window
        self.prev_block.timestamp = 200_000  # > window 100_000
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=p_pk, withdrawer_pk=w_pk,
            counter=6, amount=400, payer_key=p_key,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=p_pk, counterparty=w_pk, payer=p_pk,
            withdraw_counter=6, withdraw_amount=400,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_payer_account_missing_fails(self):
        w_pk, w_key = seed_sender_account(self.block, balance=1_000_000)
        payer_key = Ed25519PrivateKey.generate()
        payer_pk = payer_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        # do NOT create the payer account
        signature = _make_withdraw_signature(
            chain_id=1, payer_pk=payer_pk, withdrawer_pk=w_pk,
            counter=6, amount=400, payer_key=payer_key,
        )
        tx = create_transaction(
            chain_id=1, counter=0, sender=w_pk, amount=0, code=TransactionCode.CHANNEL_WITHDRAW,
            recipient=payer_pk, counterparty=w_pk, payer=payer_pk,
            withdraw_counter=6, withdraw_amount=400,
            withdraw_signature=signature, secret_key=w_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

if __name__ == "__main__":
    unittest.main()
