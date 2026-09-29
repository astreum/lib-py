"""Lifecycle tests for the generic claim_guarantee helper (treasury/guarantees.py).

Drives `claim_guarantee` directly against a RadixTree (no `apply_transaction`,
no real loan) with a synthetic claimant id, verifying the guarantee's fields via
direct trie lookup at each step. A claim is permanent: once claimed, a
guarantee stays claimed in the guarantor's record forever — there is no freeing.
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

from astreum.consensus.transaction.treasury.guarantees import claim_guarantee
from astreum.consensus.transaction.treasury.record import TreasuryGuarantee
from astreum.expression import ZERO32
from astreum.storage.radix import RadixTree, get_from_radix_tree, put_in_radix_tree

from _helpers import _FakeNode


class TestTreasuryGuaranteeLifecycle(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.guarantees_trie = RadixTree(root_hash=None)
        self.guarantee_tx_id = os.urandom(32)
        self.claimant_id = os.urandom(32)

        guarantee = TreasuryGuarantee(amount=100, duration=8, price=5, expiry=50)
        guarantee_head = guarantee.expr().hash()
        self.node.hot_storage[guarantee_head] = guarantee.expr()
        put_in_radix_tree(self.guarantees_trie, self.node, self.guarantee_tx_id, guarantee_head)

    def _read_guarantee(self):
        head = get_from_radix_tree(self.guarantees_trie, self.node, self.guarantee_tx_id)
        return TreasuryGuarantee.from_storage(self.node, head)

    def _claim(self, *, claimant_id, current_height, guarantee_transaction_id=None):
        """Call claim_guarantee and store its result the way a real caller would
        (claim_guarantee itself only writes the trie leaf's head hash — the
        caller is responsible for persisting the updated guarantee's expr, same
        as block.pending_exprs / flush_pending does for TREASURY_GUARANTEE)."""
        updated = claim_guarantee(
            guarantees_trie=self.guarantees_trie,
            node=self.node,
            guarantee_transaction_id=guarantee_transaction_id or self.guarantee_tx_id,
            claimant_id=claimant_id,
            current_height=current_height,
        )
        if updated is not None:
            self.node.hot_storage[updated.expr().hash()] = updated.expr()
        return updated

    def test_available_to_claimed(self):
        before = self._read_guarantee()
        self.assertEqual(before.claimed_by, ZERO32)

        updated = self._claim(claimant_id=self.claimant_id, current_height=10)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.claimed_by, self.claimant_id)

        after = self._read_guarantee()
        self.assertEqual(after.claimed_by, self.claimant_id)
        # Other fields unchanged.
        self.assertEqual(after.amount, 100)
        self.assertEqual(after.duration, 8)
        self.assertEqual(after.price, 5)
        self.assertEqual(after.expiry, 50)

    def test_second_claim_attempt_fails(self):
        self._claim(claimant_id=self.claimant_id, current_height=10)
        other_claimant = os.urandom(32)
        result = self._claim(claimant_id=other_claimant, current_height=11)
        self.assertIsNone(result)

        # Claim stays with the original claimant — permanent, no freeing.
        after = self._read_guarantee()
        self.assertEqual(after.claimed_by, self.claimant_id)

    def test_claim_after_expiry_fails(self):
        result = self._claim(claimant_id=self.claimant_id, current_height=50)
        self.assertIsNone(result)

        after = self._read_guarantee()
        self.assertEqual(after.claimed_by, ZERO32)

    def test_claim_missing_guarantee_fails(self):
        missing_tx_id = os.urandom(32)
        result = self._claim(
            claimant_id=self.claimant_id,
            current_height=1,
            guarantee_transaction_id=missing_tx_id,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
