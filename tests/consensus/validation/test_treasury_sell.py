"""Validation tests for the TREASURY_SELL (0x24) transaction code.

A limit seller posts a fixed-size, fixed-duration, fixed-price offer into
their own `TreasuryUserRecord.offers_root_hash` trie. No principal moves on
a post — the offer only becomes economically active when a consumer claims
it (out of scope for this plan).
"""

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
from astreum.consensus.transaction.treasury.record import TreasuryCreditOffer, TreasuryUserRecord
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree
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

LIMIT = 1000
DURATION = 8  # pow2
PRICE = 50
EXPIRY = 100


class TestTreasurySell(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block, height=1)
        seed_storage_account(self.block)
        seed_treasury_account(self.node, self.block, treasury_balance=0)

    def _make_sell_tx(self, sender_pk, sender_key, *, limit=LIMIT, duration=DURATION, price=PRICE, expiry=EXPIRY):
        return create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=TREASURY_ADDRESS,
            code=TransactionCode.TREASURY_SELL,
            limit=limit, duration=duration, price=price, expiry=expiry,
            secret_key=sender_key,
        )

    # --- success ---

    def test_sell_posts_offer_into_offers_root_hash(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        tx = self._make_sell_tx(sender_pk, sender_key)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        self.assertIsNotNone(user)
        self.assertNotEqual(user.offers_root_hash, ZERO32)

        offers_trie = RadixTree(root_hash=bytes(user.offers_root_hash))
        offer_head = get_from_radix_tree(offers_trie, self.node, tx_hash)
        offer = TreasuryCreditOffer.from_storage(self.node, offer_head)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.limit, LIMIT)
        self.assertEqual(offer.duration, DURATION)
        self.assertEqual(offer.price, PRICE)
        self.assertEqual(offer.expiry, EXPIRY)
        self.assertEqual(offer.claimed_by, ZERO32)

    def test_sell_creates_zero_balance_record_on_first_post(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        tx = self._make_sell_tx(sender_pk, sender_key)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        flush_pending(self.node, self.block)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        user_head = get_from_radix_tree(treasury.data, self.node, sender_pk)
        user = TreasuryUserRecord.from_storage(self.node, user_head)
        self.assertIsNotNone(user)
        self.assertEqual(user.balance, 0)
        self.assertEqual(user.loans_root_hash, ZERO32)

    # --- failures ---

    def test_recipient_not_treasury_fails(self):
        import os

        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        tx = create_transaction(
            chain_id=1, counter=0, sender=sender_pk, recipient=os.urandom(32),
            code=TransactionCode.TREASURY_SELL,
            limit=LIMIT, duration=DURATION, price=PRICE, expiry=EXPIRY,
            secret_key=sender_key,
        )
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_non_pow2_duration_fails_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        with self.assertRaises(ValueError):
            self._make_sell_tx(sender_pk, sender_key, duration=7)

    def test_non_positive_limit_fails_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        with self.assertRaises(ValueError):
            self._make_sell_tx(sender_pk, sender_key, limit=0)

    def test_negative_price_fails_at_create(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        with self.assertRaises(ValueError):
            self._make_sell_tx(sender_pk, sender_key, price=-1)

    def test_already_expired_expiry_fails(self):
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        # block.height == 1; expiry must be > current height.
        tx = self._make_sell_tx(sender_pk, sender_key, expiry=1)
        tx_hash = store_tx(self.node, tx)

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)

    def test_colliding_transaction_hash_fails(self):
        # Unreachable in practice (tx hashes are unique), but keeps the guard
        # consistent with borrow.py's collision check.
        sender_pk, sender_key = seed_sender_account(self.block, balance=10_000)
        tx = self._make_sell_tx(sender_pk, sender_key)
        tx_hash = store_tx(self.node, tx)

        treasury = self.block.accounts.get_account(TREASURY_ADDRESS, self.node)
        from astreum.storage.radix import put_in_radix_tree
        from astreum.storage.radix import get_radix_node_expr
        from astreum.storage.radix.node import radix_node_hash

        existing_offer = TreasuryCreditOffer(limit=1, duration=1, price=0, expiry=999)
        offers_trie = RadixTree(root_hash=None)
        put_in_radix_tree(offers_trie, self.node, tx_hash, existing_offer.expr().hash())
        self.node.hot_storage[existing_offer.expr().hash()] = existing_offer.expr()
        for trie_node in offers_trie.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)

        user_record = TreasuryUserRecord(offers_root_hash=offers_trie.root_hash or ZERO32)
        rec_head = user_record.expr().hash()
        self.node.hot_storage[rec_head] = user_record.expr()
        put_in_radix_tree(treasury.data, self.node, sender_pk, rec_head)
        for trie_node in treasury.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        treasury.data_hash = treasury.data.root_hash or ZERO32

        apply_transaction(self.node, self.block, tx_hash)
        self.assertEqual(self.block.receipts[-1].status, STATUS_FAILED)


if __name__ == "__main__":
    unittest.main()
