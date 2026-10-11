"""Tests for BloomFilter: insert and test with specific element counts."""
import os
import random
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.crypto.bloom_filter import BloomFilter, bloom_insert, bloom_test
from astreum.crypto.bloom_filter.insert import K, _bloom_positions, bloom_digest


class TestBloomFilterCounts(unittest.TestCase):

    def _test_count(self, n: int):
        bf = BloomFilter()
        elements = [os.urandom(32) for _ in range(n)]

        for e in elements:
            bloom_insert(bf, e)

        self.assertEqual(bf.count, n, f"count should be {n}")

        # No false negatives
        for e in elements:
            self.assertTrue(bloom_test(bf, e), f"element should test positive")

        # Expected tier count
        cum = 0
        expected_tiers = 0
        cap = 1
        while cum < n:
            cum += cap
            expected_tiers += 1
            cap = 1 << expected_tiers
        self.assertEqual(len(bf.tiers), expected_tiers,
                         f"expected {expected_tiers} tiers, got {len(bf.tiers)}")

    def test_count_1(self):
        self._test_count(1)

    def test_count_2(self):
        self._test_count(2)

    def test_count_3(self):
        self._test_count(3)

    def test_count_7(self):
        self._test_count(7)

    def test_count_17(self):
        self._test_count(17)

    def test_count_31(self):
        self._test_count(31)

    def test_count_61(self):
        self._test_count(61)

    def test_count_127(self):
        self._test_count(127)

    def test_count_257(self):
        self._test_count(257)


class TestBloomFilterRate(unittest.TestCase):

    def _rate(self, n: int, probes: int = 20000) -> float:
        rng = random.Random(1000 + n)
        bf = BloomFilter()
        for _ in range(n):
            bloom_insert(bf, rng.randbytes(160))
        hits = sum(bloom_test(bf, rng.randbytes(160)) for _ in range(probes))
        return hits / probes

    def test_false_positive_rate_1(self):
        self.assertLess(self._rate(1), 0.015)

    def test_false_positive_rate_7(self):
        self.assertLess(self._rate(7), 0.015)

    def test_false_positive_rate_100(self):
        self.assertLess(self._rate(100), 0.015)

    def test_false_positive_rate_1000(self):
        self.assertLess(self._rate(1000), 0.015)

    def test_positions_in_range_and_count(self):
        digest = bloom_digest(b"x" * 160)
        for bits in (16, 32, 64, 1 << 20):
            positions = _bloom_positions(digest, bits)
            self.assertEqual(len(positions), K)
            self.assertTrue(all(0 <= p < bits for p in positions))

    def test_positions_deterministic(self):
        digest = bloom_digest(b"y" * 160)
        self.assertEqual(_bloom_positions(digest, 640), _bloom_positions(digest, 640))

    def test_tier_bytes(self):
        bf = BloomFilter()
        for i in range(31):
            bloom_insert(bf, os.urandom(32))
        self.assertEqual([len(t) for t in bf.tiers], [2, 4, 8, 16, 32])


if __name__ == "__main__":
    unittest.main()
