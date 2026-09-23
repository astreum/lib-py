"""Lifecycle tests for the generic claim_offer helper (treasury/offers.py).

Drives `claim_offer` directly against a RadixTree (no `apply_transaction`,
no real loan) with a synthetic claimant id, verifying the offer's fields via
direct trie lookup at each step. A claim is permanent: once claimed, an
offer stays claimed in the seller's record forever — there is no freeing.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
VALIDATION_HELPERS_DIR = ROOT / "tests" / "consensus" / "validation"
if str(VALIDATION_HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATION_HELPERS_DIR))

from astreum.consensus.transaction.treasury.offers import claim_offer
from astreum.consensus.transaction.treasury.record import TreasuryCreditOffer
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree

from _helpers import _FakeNode


class TestTreasuryOfferLifecycle(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.offers_trie = RadixTree(root_hash=None)
        self.offer_tx_id = os.urandom(32)
        self.claimant_id = os.urandom(32)

        offer = TreasuryCreditOffer(limit=100, duration=8, price=5, expiry=50)
        offer_head = offer.expr().hash()
        self.node.hot_storage[offer_head] = offer.expr()
        put_in_radix_tree(self.offers_trie, self.node, self.offer_tx_id, offer_head)

    def _read_offer(self):
        head = get_from_radix_tree(self.offers_trie, self.node, self.offer_tx_id)
        return TreasuryCreditOffer.from_storage(self.node, head)

    def _claim(self, *, claimant_id, current_height, offer_transaction_id=None):
        """Call claim_offer and store its result the way a real caller would
        (claim_offer itself only writes the trie leaf's head hash — the
        caller is responsible for persisting the updated offer's expr, same
        as block.pending_exprs / flush_pending does for TREASURY_SELL)."""
        updated = claim_offer(
            offers_trie=self.offers_trie,
            node=self.node,
            offer_transaction_id=offer_transaction_id or self.offer_tx_id,
            claimant_id=claimant_id,
            current_height=current_height,
        )
        if updated is not None:
            self.node.hot_storage[updated.expr().hash()] = updated.expr()
        return updated

    def test_available_to_claimed(self):
        before = self._read_offer()
        self.assertEqual(before.claimed_by, ZERO32)

        updated = self._claim(claimant_id=self.claimant_id, current_height=10)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.claimed_by, self.claimant_id)

        after = self._read_offer()
        self.assertEqual(after.claimed_by, self.claimant_id)
        # Other fields unchanged.
        self.assertEqual(after.limit, 100)
        self.assertEqual(after.duration, 8)
        self.assertEqual(after.price, 5)
        self.assertEqual(after.expiry, 50)

    def test_second_claim_attempt_fails(self):
        self._claim(claimant_id=self.claimant_id, current_height=10)
        other_claimant = os.urandom(32)
        result = self._claim(claimant_id=other_claimant, current_height=11)
        self.assertIsNone(result)

        # Claim stays with the original claimant — permanent, no freeing.
        after = self._read_offer()
        self.assertEqual(after.claimed_by, self.claimant_id)

    def test_claim_after_expiry_fails(self):
        result = self._claim(claimant_id=self.claimant_id, current_height=50)
        self.assertIsNone(result)

        after = self._read_offer()
        self.assertEqual(after.claimed_by, ZERO32)

    def test_claim_missing_offer_fails(self):
        missing_tx_id = os.urandom(32)
        result = self._claim(
            claimant_id=self.claimant_id,
            current_height=1,
            offer_transaction_id=missing_tx_id,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
