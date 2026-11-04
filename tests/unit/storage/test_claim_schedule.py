"""Claim schedule: lane plan, due heights and the due-height queue."""

import sys
import unittest
from fractions import Fraction

sys.path.insert(0, "src")

from astreum.consensus.transaction.storage.model import StorageLane, StorageRecord
from astreum.expression import ZERO32
from astreum.storage.workers.claim_schedule import (
    ClaimQueue,
    LaneChoice,
    affordable_bits,
    claim_cost,
    holder_due,
    min_elapsed,
    plan_record,
    pow_budget,
    queue_add,
    queue_pop_due,
    queue_push,
    takeover_due,
)

ID = b"\x00" * 32
ME = b"\x00\xff" + b"\x00" * 30
NEAR = b"\x00\x00\xff" + b"\x00" * 29
FAR = b"\x80" + b"\x00" * 31


def _record(winner0, height0, winner1, height1):
    return StorageRecord(
        new_size=100,
        lanes=(
            StorageLane(b"\x01" * 32, height0, winner0),
            StorageLane(b"\x02" * 32, height1, winner1),
        ),
    )


def _plan(record, head, difficulty, fair=None, delay=2, affordable=15, elapsed=0):
    return plan_record(
        record, ME, ID, head, difficulty, fair, delay, affordable, elapsed
    )


class TestScheduleMath(unittest.TestCase):
    def test_pow_budget(self):
        self.assertEqual(pow_budget(1000.0, 64, 2), 64000)
        self.assertEqual(pow_budget(1000.0, None, 2), 1)
        self.assertEqual(pow_budget(0.0, 64, 1), 1)

    def test_affordable_bits(self):
        self.assertEqual(affordable_bits(64000), 15)
        self.assertEqual(affordable_bits(1), 0)

    def test_claim_cost(self):
        self.assertEqual(claim_cost(Fraction(1), 100, 2), 200 + 721)

    def test_min_elapsed(self):
        self.assertEqual(min_elapsed(100, 921, 1000, 2), 15)
        self.assertEqual(min_elapsed(100, 0, 0, 1), 0)

    def test_holder_due(self):
        self.assertEqual(holder_due(100, 50, 2), 148)
        self.assertEqual(holder_due(100, None, 2), 100)

    def test_takeover_due(self):
        self.assertEqual(takeover_due(100, 20, 0, 15), 132)
        self.assertEqual(takeover_due(100, 20, 8, 15), 101)
        self.assertEqual(takeover_due(100, 20, -2, 15), 228)


class TestPlanRecord(unittest.TestCase):
    def test_plan_holder_not_due(self):
        record = _record(ME, 100, ZERO32, 0)
        self.assertEqual(_plan(record, 120, 0, fair=50), (None, 148))

    def test_plan_holder_due(self):
        record = _record(ME, 100, ZERO32, 0)
        self.assertEqual(_plan(record, 148, 0, fair=50), (LaneChoice(0, 0), 148))

    def test_plan_holder_waits_for_profit(self):
        record = _record(ME, 100, ZERO32, 0)
        self.assertEqual(_plan(record, 148, 0, fair=50, elapsed=60), (None, 160))

    def test_plan_holder_profit_due_without_interval(self):
        record = _record(ME, 100, ZERO32, 0)
        self.assertEqual(_plan(record, 120, 0, elapsed=10), (LaneChoice(0, 0), 110))

    def test_plan_holder_never_takes_other_lane(self):
        record = _record(ME, 100, FAR, 0)
        choice, _ = _plan(record, 200, 0, fair=50)
        self.assertIn(choice, (None, LaneChoice(0, 0)))

    def test_plan_takeover_fewest_bits(self):
        record = _record(NEAR, 0, FAR, 0)
        choice, _ = _plan(record, 15, 20)
        self.assertEqual(choice, LaneChoice(1, 8))

    def test_plan_takeover_too_expensive(self):
        record = _record(NEAR, 0, FAR, 0)
        self.assertEqual(_plan(record, 15, 40), (None, 131072))

    def test_plan_takeover_waits_for_profit(self):
        record = _record(NEAR, 0, FAR, 0)
        self.assertEqual(_plan(record, 15, 20, elapsed=100), (None, 100))

    def test_plan_takeover_profit_boundary(self):
        record = _record(NEAR, 0, FAR, 0)
        self.assertEqual(_plan(record, 14, 20, elapsed=15), (None, 15))
        choice, _ = _plan(record, 15, 20, elapsed=15)
        self.assertEqual(choice, LaneChoice(1, 8))

    def test_plan_takeover_tie_most_elapsed(self):
        record = _record(FAR, 1, FAR[:-1] + b"\x01", 0)
        choice, _ = _plan(record, 19, 20)
        self.assertEqual(choice.lane, 1)


class TestClaimQueue(unittest.TestCase):
    def test_queue_pop_in_due_order(self):
        queue = ClaimQueue()
        a, b = b"a" * 32, b"b" * 32
        queue_add(queue, b, 20)
        queue_add(queue, a, 10)
        self.assertEqual(queue_pop_due(queue, 30, 5), [a, b])

    def test_queue_pop_limit_and_head(self):
        queue = ClaimQueue()
        a, b, c = b"a" * 32, b"b" * 32, b"c" * 32
        queue_add(queue, a, 10)
        queue_add(queue, b, 11)
        queue_add(queue, c, 50)
        self.assertEqual(queue_pop_due(queue, 30, 1), [a])
        self.assertEqual(queue_pop_due(queue, 30, 1), [b])
        self.assertEqual(queue_pop_due(queue, 30, 1), [])

    def test_queue_add_skips_known(self):
        queue = ClaimQueue()
        a = b"a" * 32
        self.assertTrue(queue_add(queue, a, 10))
        self.assertFalse(queue_add(queue, a, 10))
        self.assertEqual(len(queue.heap), 1)

    def test_queue_push_requeues_known(self):
        queue = ClaimQueue()
        a = b"a" * 32
        queue_add(queue, a, 10)
        self.assertEqual(queue_pop_due(queue, 10, 1), [a])
        queue_push(queue, a, 20)
        self.assertEqual(queue_pop_due(queue, 19, 1), [])
        self.assertEqual(queue_pop_due(queue, 20, 1), [a])

    def test_queue_drops_released(self):
        queue = ClaimQueue()
        a = b"a" * 32
        queue_add(queue, a, 10)
        queue.released.add(a)
        self.assertEqual(queue_pop_due(queue, 30, 5), [])


if __name__ == "__main__":
    unittest.main()
