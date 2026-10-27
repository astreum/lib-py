"""The block time is the end of the first slot the build can finish in."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.validation.validator import (  # noqa: E402
    SLOT_DURATION_SECONDS,
    estimate_build_seconds,
    next_block_time,
)


class TestEstimateBuildSeconds(unittest.TestCase):
    def test_estimate_without_history(self):
        self.assertEqual(estimate_build_seconds(0, 0), 1.0)

    def test_estimate_without_earlier_build(self):
        self.assertEqual(estimate_build_seconds(21000, 0), 22.0)

    def test_estimate_steady_builds(self):
        self.assertEqual(estimate_build_seconds(21000, 21000), 22.0)

    def test_estimate_adds_growth(self):
        self.assertEqual(estimate_build_seconds(21000, 20000), 23.0)

    def test_estimate_ignores_a_faster_build(self):
        self.assertEqual(estimate_build_seconds(21000, 40000), 22.0)

    def test_estimate_has_no_upper_bound(self):
        self.assertEqual(estimate_build_seconds(10_000_000, 9_000_000), 11001.0)


class TestNextBlockTime(unittest.TestCase):
    def test_healthy_chain_uses_the_next_slot(self):
        self.assertEqual(next_block_time(1000, 1000.1, 1.0), 1002)

    def test_slow_build_uses_a_later_slot(self):
        self.assertEqual(next_block_time(1000, 1000.1, 22.0), 1024)

    def test_result_is_a_whole_number_of_slots_after_the_parent(self):
        for now in (1000.0, 1000.1, 1001.9, 1003.5, 1017.25):
            for estimate in (0.0, 1.0, 7.3, 22.0):
                result = next_block_time(1000, now, estimate)
                self.assertEqual((result - 1000) % SLOT_DURATION_SECONDS, 0)
                self.assertGreaterEqual(result, now + estimate)

    def test_never_earlier_than_one_slot(self):
        self.assertEqual(next_block_time(2000, 1000.2, 1.0), 2002)

    def test_long_stall(self):
        now = 10_000.0 + 7200
        result = next_block_time(10_000, now, 22.0)
        self.assertGreaterEqual(result, now + 22.0)
        self.assertLess(result, now + 22.0 + SLOT_DURATION_SECONDS)

    def test_slot_length_is_a_parameter(self):
        self.assertEqual(next_block_time(1000, 1000.1, 1.0, slot=5), 1005)


if __name__ == "__main__":
    unittest.main()
