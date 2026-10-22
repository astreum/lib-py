"""Tests for top-level transaction counter enforcement in _apply_tx_effects.

Convention (matching the tx.new operator): a valid tx carries
counter == sender_account.counter at processing time; processing increments
the account counter. Mismatched txs fail the receipt (fees still charged)
without consuming the counter.
"""

import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.account import create_account
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.block import Block
from astreum.consensus.models.receipt import STATUS_SUCCESS, STATUS_FAILED
from astreum.consensus.transaction import apply_transaction
from astreum.consensus.transaction.code import TransactionCode

from tests.unit.consensus.transaction.test_apply import (
    _FakeNode,
    _make_block,
    _make_previous_block,
    _make_tx,
    _seed_storage_account,
    _store_tx,
)


def _seed_sender(block: Block, node, balance: int = 1_000_000):
    key = Ed25519PrivateKey.generate()
    pk = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    block.accounts.set_account(pk, create_account(balance=balance))
    return pk, key


class TestCounterEnforcement(unittest.TestCase):

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = _make_previous_block()
        self.block = _make_block(self.node, self.prev_block)
        _seed_storage_account(self.block)

    def _sender(self, balance=1_000_000):
        return _seed_sender(self.block, self.node, balance=balance)

    def test_match_applies_and_increments(self):
        """counter == account.counter applies; account counter increments."""
        sender_pk, sender_key = self._sender()
        assert self.block.accounts.get_account(sender_pk, self.node).counter == 0

        tx = _make_tx(
            chain_id=1,
            sender_pk=sender_pk,
            recipient=b"\x02" * 32,
            amount=100,
            secret_key=sender_key,
            counter=0,
        )
        apply_transaction(self.node, self.block, _store_tx(self.node, tx))

        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 1
        )

    def test_mismatch_fails_without_consuming_counter(self):
        """Wrong counter -> STATUS_FAILED, fees charged, counter unchanged."""
        sender_pk, sender_key = self._sender()
        recipient = b"\x02" * 32

        tx = _make_tx(
            chain_id=1,
            sender_pk=sender_pk,
            recipient=recipient,
            amount=100,
            secret_key=sender_key,
            counter=5,  # account counter is 0
        )
        apply_transaction(self.node, self.block, _store_tx(self.node, tx))

        receipt = self.block.receipts[-1]
        sender = self.block.accounts.get_account(sender_pk, self.node)
        self.assertEqual(receipt.status, STATUS_FAILED)
        self.assertEqual(sender.counter, 0)  # not consumed
        # Fees still charged (plus new-recipient storage fee added after the
        # receipt was built), transfer not applied.
        self.assertLess(sender.balance, 1_000_000 - receipt.transaction_fee)
        recipient_account = self.block.accounts.get_account(recipient, self.node)
        self.assertTrue(recipient_account is None or recipient_account.balance == 0)

    def test_failed_tx_still_consumes_counter(self):
        """Valid counter + later failure -> receipt failed, counter increments,
        replay of the same tx now fails on counter."""
        sender_pk, sender_key = self._sender(balance=1_000_000)

        tx = _make_tx(
            chain_id=1,
            sender_pk=sender_pk,
            recipient=STORAGE_ADDRESS,
            amount=100,  # amount to burn with STORAGE_CREATE -> failed receipt
            code=TransactionCode.STORAGE_CREATE,
            secret_key=sender_key,
            counter=0,
        )
        tx_hash = _store_tx(self.node, tx)
        apply_transaction(self.node, self.block, tx_hash)

        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)
        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 1
        )

        # Replay the same tx: counter now 1, tx carries 0 -> fails on counter.
        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)
        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 1
        )

    def test_sequence_of_three_then_replay_first(self):
        """Counters n, n+1, n+2 all apply; re-submitting the first fails."""
        sender_pk, sender_key = self._sender()
        recipient = b"\x02" * 32

        hashes = []
        for counter in (0, 1, 2):
            tx = _make_tx(
                chain_id=1,
                sender_pk=sender_pk,
                recipient=recipient,
                amount=10,
                secret_key=sender_key,
                counter=counter,
            )
            tx_hash = _store_tx(self.node, tx)
            apply_transaction(self.node, self.block, tx_hash)
            self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)
            hashes.append(tx_hash)

        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 3
        )

        # Re-apply the first tx: its counter (0) no longer matches (3).
        apply_transaction(self.node, self.block, hashes[0])
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_same_tx_twice_in_one_block(self):
        """Applying the same tx twice: second application fails on counter."""
        sender_pk, sender_key = self._sender()
        tx = _make_tx(
            chain_id=1,
            sender_pk=sender_pk,
            recipient=b"\x02" * 32,
            amount=10,
            secret_key=sender_key,
            counter=0,
        )
        tx_hash = _store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_SUCCESS)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)
        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 1
        )

    def test_nested_skips_check_and_increment(self):
        """nested=True: no counter check, no increment (tx.new owns it)."""
        from astreum.consensus.transaction.apply import _apply_tx_effects

        sender_pk, sender_key = self._sender()
        tx = _make_tx(
            chain_id=1,
            sender_pk=sender_pk,
            recipient=b"\x02" * 32,
            amount=10,
            secret_key=sender_key,
            counter=99,  # deliberately wrong; nested must not care
        )

        status, _, _, _ = _apply_tx_effects(
            self.node, self.block, tx, _store_tx(self.node, tx), nested=True
        )
        self.assertEqual(status, STATUS_SUCCESS)
        self.assertEqual(
            self.block.accounts.get_account(sender_pk, self.node).counter, 0
        )


if __name__ == "__main__":
    unittest.main()
