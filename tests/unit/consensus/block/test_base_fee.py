"""Estimator for the transaction base fee bid."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.block import base_fee as module  # noqa: E402
from astreum.consensus.block.base_fee import (  # noqa: E402
    estimate_transaction_base_fee,
    mean_base_fee_per_transaction,
)


def _block(height, total_base_fee, transaction_count):
    return SimpleNamespace(
        height=height,
        total_base_fee=total_base_fee,
        transaction_count=transaction_count,
    )


class TestMeanBaseFeePerTransaction(unittest.TestCase):
    def test_an_empty_block_returns_zero(self):
        self.assertEqual(mean_base_fee_per_transaction(_block(5, 0, 0)), 0)

    def test_the_mean_of_the_paid_bids(self):
        self.assertEqual(mean_base_fee_per_transaction(_block(5, 10, 4)), 2)

    def test_a_block_with_a_single_one_aster_bid_returns_one(self):
        self.assertEqual(mean_base_fee_per_transaction(_block(5, 7, 7)), 1)


class TestEstimateTransactionBaseFee(unittest.TestCase):
    def _run(self, blocks, head_height, *, window=None, samples=None):
        head = SimpleNamespace(height=head_height)
        node = SimpleNamespace(
            config=SimpleNamespace(
                storage_claim_fee_window=2,
                storage_claim_fee_samples=2,
            )
        )
        targets = []
        self.previous_calls = 0

        def previous(_node, block):
            self.previous_calls += 1
            return blocks.get(block.height - 1)

        def find(_node, *, starting_block, target_height):
            targets.append(target_height)
            return blocks.get(target_height)

        with mock.patch.object(module, "get_previous_block", side_effect=previous), \
             mock.patch.object(module, "find_block_by_height", side_effect=find):
            value = estimate_transaction_base_fee(
                node, head, window=window, samples=samples
            )
        return value, targets

    def test_the_estimate_is_a_weighted_mean(self):
        blocks = {9: _block(9, 100, 10), 8: _block(8, 1, 1)}
        value, _ = self._run(blocks, 10)
        self.assertEqual(value, 9)

    def test_empty_blocks_do_not_drag_the_mean_down(self):
        blocks = {9: _block(9, 0, 0), 8: _block(8, 0, 0)}
        value, _ = self._run(blocks, 10)
        self.assertEqual(value, 1)

    def test_a_short_chain_returns_a_value(self):
        value, targets = self._run({}, 1)
        self.assertEqual(value, 1)
        self.assertEqual(targets, [])

    def test_it_samples_the_configured_window_when_no_window_is_given(self):
        blocks = {9: _block(9, 100, 10), 8: _block(8, 1, 1)}
        value, _ = self._run(blocks, 10, window=None, samples=None)
        self.assertEqual(value, 9)

    def test_the_stride_is_coprime_with_the_era(self):
        # 64 // 4 = 16, and gcd(16, 1024) is 16, so the stride drops to 15.
        blocks = {h: _block(h, 1, 1) for h in range(1, 41)}
        _, targets = self._run(blocks, 41, window=64, samples=4)
        self.assertEqual(targets, [25, 10])

    def test_the_default_stride_is_1023(self):
        # 65,536 // 64 = 1,024, which aliases every era, so it drops to 1,023.
        blocks = {h: _block(h, 1, 1) for h in range(1, 65_536)}
        _, targets = self._run(blocks, 65_536, window=65_536, samples=64)
        self.assertEqual(targets[0], 65_536 - 1_024)
        self.assertEqual(targets[0] - targets[1], 1_023)

    def test_the_walk_costs_one_parent_and_one_lookup_per_sample(self):
        blocks = {h: _block(h, 1, 1) for h in range(1, 65_536)}
        _, targets = self._run(blocks, 65_536, window=65_536, samples=64)
        self.assertEqual(self.previous_calls, 1)
        self.assertEqual(len(targets), 63)

    def test_it_reads_one_block_per_sample(self):
        blocks = {h: _block(h, 1, 1) for h in range(1, 11)}
        value, targets = self._run(blocks, 11, window=4, samples=4)
        self.assertEqual(value, 1)
        self.assertEqual(targets, [9, 8, 7])

    def test_it_samples_down_to_genesis(self):
        blocks = {1: _block(1, 4, 2), 0: _block(0, 0, 0)}
        value, targets = self._run(blocks, 2, window=10, samples=10)
        self.assertEqual(value, 2)
        self.assertEqual(targets, [0])


if __name__ == "__main__":
    unittest.main()
