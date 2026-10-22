"""Claim worker lane choice: which lane of a record a node claims."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.consensus.transaction.storage.model import StorageLane, StorageRecord
from astreum.crypto.bloom_search import ERA_SIZE
from astreum.expression import ZERO32
from astreum.storage.workers import claim as claim_mod
from astreum.storage.workers.claim import _build_claims_for_records, _claim_expr

ME = b"\x01" * 32
OTHER_A = b"\x02" * 32
OTHER_B = b"\x03" * 32
STORAGE_ID = b"\x0c" * 32


class _AccountsStub:
    def get_account(self, address=None, node=None):
        return SimpleNamespace(data=None)


class TestClaimLanes(unittest.TestCase):
    def _record(self, winner0, height0, winner1, height1):
        return StorageRecord(
            new_size=100,
            new_count=1,
            lanes=(
                StorageLane(block_hash=b"\x11" * 32, height=height0, winner=winner0),
                StorageLane(block_hash=b"\x12" * 32, height=height1, winner=winner1),
            ),
        )

    def _run(self, record, height, spacing=None):
        node = SimpleNamespace(storage_public_key_bytes=ME)
        latest_block = SimpleNamespace(height=height, accounts=_AccountsStub())
        spacing = {} if spacing is None else spacing
        with patch.object(
            claim_mod, "iter_records_in_cold_storage", return_value=iter([STORAGE_ID])
        ), patch.object(
            claim_mod, "get_from_radix_tree", return_value=SimpleNamespace(hash=lambda: b"\x0e" * 32)
        ), patch.object(
            claim_mod, "storage_record_from_storage", return_value=record
        ), patch.object(
            claim_mod,
            "_compute_pow_and_challenge",
            side_effect=lambda n, sid, rec, lane: (sid, b"slot", [b"\x01" * 32], 7, lane),
        ):
            return _build_claims_for_records(node, latest_block, spacing)

    def test_incumbent_claims_own_lane(self):
        for mine in (0, 1):
            with self.subTest(lane=mine):
                winners = [OTHER_A, OTHER_A]
                winners[mine] = ME
                record = self._record(winners[0], 0, winners[1], 0)
                entries = self._run(record, 2 * ERA_SIZE)
                self.assertEqual([e[4] for e in entries], [mine])
                self.assertEqual(entries[0][5], 100 * 2 * ERA_SIZE)

    def test_incumbent_waits_for_spacing(self):
        record = self._record(ME, 0, OTHER_A, 0)
        self.assertEqual(self._run(record, ERA_SIZE // 2), [])

    def test_no_takeover_before_five_eras(self):
        record = self._record(OTHER_A, 0, OTHER_B, 0)
        self.assertEqual(self._run(record, 4 * ERA_SIZE), [])

    def test_takeover_after_five_eras(self):
        record = self._record(OTHER_A, 0, OTHER_B, 0)
        entries = self._run(record, 5 * ERA_SIZE)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][4], 0)  # lane 0 wins a tie

    def test_takeover_picks_oldest_lane(self):
        # Lane 0 paid 6 eras ago, lane 1 paid 8 eras ago.
        record = self._record(OTHER_A, 2 * ERA_SIZE, OTHER_B, 0)
        entries = self._run(record, 8 * ERA_SIZE)
        self.assertEqual([e[4] for e in entries], [1])
        self.assertEqual(entries[0][5], 100 * 8 * ERA_SIZE)

    def test_takeover_skips_a_lane_that_is_too_young(self):
        record = self._record(OTHER_A, 0, OTHER_B, 4 * ERA_SIZE)
        entries = self._run(record, 6 * ERA_SIZE)
        self.assertEqual([e[4] for e in entries], [0])

    def test_holder_never_takes_other_lane(self):
        # This node holds lane 0, which is too young for another claim. Lane 1
        # is old, but a holder never takes it.
        record = self._record(ME, 5 * ERA_SIZE, OTHER_A, 0)
        self.assertEqual(self._run(record, 6 * ERA_SIZE, spacing={STORAGE_ID: 4}), [])

    def test_claim_has_five_items_with_lane(self):
        record = self._record(OTHER_A, 0, OTHER_B, 0)
        entries = self._run(record, 6 * ERA_SIZE)
        claim = entries[0][:5]
        self.assertEqual(len(claim), 5)
        self.assertEqual(claim[4], 0)
        # The claim expression carries the lane as its fifth item.
        expr = _claim_expr(claim)
        items = []
        while expr is not None and expr.base == "link" and expr.head is not None:
            items.append(expr.head)
            expr = expr.tail
        self.assertEqual(len(items), 5)


if __name__ == "__main__":
    unittest.main()
