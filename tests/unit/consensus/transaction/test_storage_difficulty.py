import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import ZERO32  # noqa: E402
from astreum.consensus.transaction.storage.difficulty import (  # noqa: E402
    distance_delta,
    fair_interval,
    next_storage_difficulty,
    parent_storage_difficulty,
    required_bits,
    shared_prefix_bits,
    takeover_position,
    takeover_vote,
)

ID = b"\x00" * 32
NEAR16 = b"\x00\x00\xff" + b"\x00" * 29
NEAR8 = b"\x00\xff" + b"\x00" * 30


def _parent(height, prev_claims):
    stats = [(0, 0, 0, 0, 0, 0)]
    for claims in prev_claims:
        stats.append((0, 0, 0, 0, claims, 0))
    return SimpleNamespace(height=height, statistics=stats)


class StorageDifficultyTests(unittest.TestCase):
    def test_shared_prefix_bits(self):
        self.assertEqual(shared_prefix_bits(NEAR16, ID), 16)
        self.assertEqual(shared_prefix_bits(NEAR8, ID), 8)
        self.assertEqual(shared_prefix_bits(ID, ID), 256)

    def test_delta_signed(self):
        self.assertEqual(distance_delta(sender=NEAR16, winner=NEAR8, storage_id=ID), 8)
        self.assertEqual(distance_delta(sender=NEAR8, winner=NEAR16, storage_id=ID), -8)
        self.assertEqual(distance_delta(sender=NEAR8, winner=ZERO32, storage_id=ID), 0)

    def test_holder_zero_bits(self):
        self.assertEqual(
            required_bits(
                sender=NEAR8, winner=NEAR8, storage_id=ID, elapsed=16, storage_difficulty=9
            ),
            0,
        )

    def test_bits_formula(self):
        kw = dict(storage_id=ID, elapsed=16, storage_difficulty=20)
        self.assertEqual(required_bits(sender=NEAR16, winner=NEAR8, **kw), 8)
        self.assertEqual(required_bits(sender=NEAR8, winner=NEAR16, **kw), 24)
        self.assertEqual(required_bits(sender=NEAR8, winner=ZERO32, **kw), 16)
        kw17 = dict(kw, elapsed=17)
        self.assertEqual(required_bits(sender=NEAR16, winner=NEAR8, **kw17), 8)
        kw1 = dict(kw, elapsed=1)
        self.assertEqual(required_bits(sender=NEAR16, winner=NEAR8, **kw1), 12)

    def test_bits_floor_zero(self):
        self.assertEqual(
            required_bits(
                sender=NEAR16,
                winner=NEAR8,
                storage_id=ID,
                elapsed=16,
                storage_difficulty=3,
            ),
            0,
        )

    def test_position(self):
        self.assertEqual(takeover_position(16, 3), 128)
        self.assertEqual(takeover_position(16, -3), 2)
        self.assertEqual(takeover_position(16, 0), 16)

    def test_vote(self):
        self.assertEqual(takeover_vote(3, 8), 1)
        self.assertEqual(takeover_vote(8, 8), 0)
        self.assertEqual(takeover_vote(9, 8), -1)
        self.assertEqual(takeover_vote(3, None), 0)

    def test_next_storage_difficulty(self):
        self.assertEqual(next_storage_difficulty(3, 4), 4)
        self.assertEqual(next_storage_difficulty(3, -2), 2)
        self.assertEqual(next_storage_difficulty(0, -1), 0)
        self.assertEqual(next_storage_difficulty(3, 0), 3)

    def test_parent_storage_difficulty(self):
        self.assertEqual(
            parent_storage_difficulty(SimpleNamespace(previous_block=None)), 0
        )
        previous = SimpleNamespace(storage_difficulty=6)
        self.assertEqual(
            parent_storage_difficulty(SimpleNamespace(previous_block=previous)), 6
        )

    def test_fair_interval_smallest_full_window(self):
        self.assertEqual(fair_interval(_parent(8, [2, 4, 8, 16])), 8)

    def test_fair_interval_picks_first_full(self):
        self.assertEqual(fair_interval(_parent(8, [2, 4, 20, 40])), 3)

    def test_fair_interval_largest_when_none_full(self):
        self.assertEqual(fair_interval(_parent(8, [1, 2, 4, 10])), 12)

    def test_fair_interval_none(self):
        self.assertIsNone(fair_interval(_parent(8, [0, 0, 0, 0])))
        self.assertIsNone(fair_interval(None))


if __name__ == "__main__":
    unittest.main()
