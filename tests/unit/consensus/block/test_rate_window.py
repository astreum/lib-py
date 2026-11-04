import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block.rate_window import (  # noqa: E402
    update_statistics,
    windowed_claims,
    windowed_rate_fraction,
)


def _old_update_statistics(height, prev_statistics, delta_fee, delta_stake):
    """Pre-seed reference implementation for regression comparison."""
    stats = []
    if prev_statistics:
        fee, stake, _, _ = prev_statistics[0]
    else:
        fee, stake = 0, 0
    stats.append((fee + delta_fee, stake + delta_stake, 0, 0))

    max_entries = height.bit_length() if height > 0 else 0
    for i in range(max_entries):
        R = 1 << i
        pi = i + 1
        if prev_statistics and pi < len(prev_statistics):
            prev_fee, prev_stake, curr_fee, curr_stake = prev_statistics[pi]
        else:
            prev_fee, prev_stake, curr_fee, curr_stake = 0, 0, 0, 0

        if height % R == 0 and height > 0:
            prev_fee, prev_stake = curr_fee, curr_stake
            curr_fee, curr_stake = delta_fee, delta_stake
        else:
            curr_fee += delta_fee
            curr_stake += delta_stake

        stats.append((prev_fee, prev_stake, curr_fee, curr_stake))
    return stats


class TestUpdateStatisticsSeed(unittest.TestCase):
    def test_newborn_top_window_seeds_prev_from_all_time(self) -> None:
        # Reach 4096 by accumulating like the chain would
        stats = None
        for h in range(1, 4096):
            stats = update_statistics(h, stats, 1, 1, 0)
        prev_statistics = stats
        new_stats = update_statistics(4096, prev_statistics, 1, 1, 0)

        self.assertEqual(len(new_stats), 14)
        top = new_stats[13]
        # prev = all-time totals at height 4095 (one full completed window)
        self.assertEqual(top[0], prev_statistics[0][0])
        self.assertEqual(top[1], prev_statistics[0][1])
        self.assertGreater(top[1], 0)
        # curr = this block's delta
        self.assertEqual(top[2], 1)
        self.assertEqual(top[3], 1)

    def test_duration_4096_rate_available_immediately(self) -> None:
        stats = None
        for h in range(1, 4096):
            stats = update_statistics(h, stats, 2, 1, 0)
        stats = update_statistics(4096, stats, 2, 1, 0)
        stats_4097 = update_statistics(4097, stats, 2, 1, 0)

        prev_block = SimpleNamespace(height=4097, statistics=stats_4097)
        block = SimpleNamespace(previous_block=prev_block)

        fraction = windowed_rate_fraction(block, 4096)
        self.assertIsNotNone(fraction)
        fee, stake = fraction
        self.assertGreater(stake, 0)
        self.assertGreater(fee, 0)

    def test_existing_windows_rotate_identically_to_old_logic(self) -> None:
        import random

        rng = random.Random(7)
        stats = None
        for h in range(1, 5200):
            delta_fee = rng.randint(0, 5)
            delta_stake = rng.randint(0, 3)
            old_stats = [entry[:4] for entry in stats] if stats else None
            stats = update_statistics(h, stats, delta_fee, delta_stake, 0)
            reference = _old_update_statistics(h, old_stats, delta_fee, delta_stake)
            # Entries 0..len(reference)-2 must match: only the newborn top
            # entry (last position) may differ from the old logic.
            for idx in range(len(reference) - 1):
                self.assertEqual(
                    stats[idx][:4],
                    reference[idx],
                    f"entry {idx} diverged at height {h}",
                )

    def test_duration_one_seeded_at_genesis(self) -> None:
        stats = update_statistics(1, None, 3, 2, 0)
        # Entry 0: all-time
        self.assertEqual(stats[0], (3, 2, 0, 0, 0, 0))
        # Entry 1 (R=1): born at height 1, no prev history to seed
        self.assertEqual(stats[1], (0, 0, 3, 2, 0, 0))

    def test_seed_uses_all_time_not_window_totals(self) -> None:
        # At height 4 (R=4 born), all-time accumulates deltas that window 4
        # never saw: prev must equal all-time at height 3.
        stats = None
        for h in range(1, 4):
            stats = update_statistics(h, stats, 10, 5, 0)
        prev_all_time = stats[0]
        new_stats = update_statistics(4, stats, 10, 5, 0)
        top = new_stats[3]
        self.assertEqual(top[0], prev_all_time[0])
        self.assertEqual(top[1], prev_all_time[1])


class TestClaimStatistics(unittest.TestCase):
    def test_claims_follow_fee_rules(self) -> None:
        stats = None
        for h in range(0, 17):
            stats = update_statistics(h, stats, h, 0, h)
            self.assertEqual(stats[0][2], stats[0][0])
            for entry in stats[1:]:
                self.assertEqual((entry[4], entry[5]), (entry[0], entry[2]))

    def test_windowed_claims_blends(self) -> None:
        stats = [(0, 0, 0, 0, 0, 0)] * 3 + [(0, 0, 0, 0, 8, 4)]
        parent = SimpleNamespace(height=6, statistics=stats)
        self.assertEqual(windowed_claims(parent, 4), 6)

    def test_windowed_claims_none(self) -> None:
        stats = [(0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0), (0, 0, 0, 0, 8, 4)]
        parent = SimpleNamespace(height=6, statistics=stats)
        self.assertIsNone(windowed_claims(parent, 3))
        self.assertIsNone(windowed_claims(None, 4))
        self.assertIsNone(windowed_claims(SimpleNamespace(height=6, statistics=[]), 4))
        self.assertIsNone(windowed_claims(parent, 8))
        self.assertIsNone(windowed_claims(parent, 0))

    def test_rate_fraction_unchanged(self) -> None:
        old_stats = [(40, 20, 0, 0), (1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12)]
        new_stats = [(e[0], e[1], e[2], e[3], 5, 6) for e in old_stats]
        for duration in (0, 1, 2, 4, 8):
            for height in (5, 6):
                old_block = SimpleNamespace(
                    previous_block=SimpleNamespace(height=height, statistics=old_stats)
                )
                new_block = SimpleNamespace(
                    previous_block=SimpleNamespace(height=height, statistics=new_stats)
                )
                self.assertEqual(
                    windowed_rate_fraction(new_block, duration),
                    windowed_rate_fraction(old_block, duration),
                )


if __name__ == "__main__":
    unittest.main()
