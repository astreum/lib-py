"""Claim flow: in flight, band, capacity and release."""

import sys
import unittest

sys.path.insert(0, "src")

from astreum.storage.workers.claim_flow import (
    CapacityLog,
    InFlight,
    band_after_inclusion,
    band_after_loss,
    capacity_min,
    capacity_record,
    in_flight_status,
    lane_value,
    lanes_to_release,
    need,
)


class ClaimFlowTests(unittest.TestCase):
    def test_status_included_when_counter_passed(self):
        self.assertEqual(in_flight_status(InFlight(5, 100, []), 6, 200), "included")

    def test_status_lost_after_two_blocks(self):
        in_flight = InFlight(5, 100, [])
        self.assertEqual(in_flight_status(in_flight, 5, 101), "pending")
        self.assertEqual(in_flight_status(in_flight, 5, 102), "lost")

    def test_status_pending_when_head_below_send_height(self):
        self.assertEqual(in_flight_status(InFlight(5, 100, []), 5, 90), "pending")

    def test_band_grows_on_next_block(self):
        self.assertEqual(band_after_inclusion(3, 1), 4)
        self.assertEqual(band_after_inclusion(3, 2), 3)

    def test_band_halves_on_loss(self):
        self.assertEqual(band_after_loss(5), 2)
        self.assertEqual(band_after_loss(1), 1)

    def test_capacity_min_window(self):
        log = CapacityLog()
        self.assertEqual(capacity_min(log, 200, 60), float("inf"))
        capacity_record(log, 100, 2.0)
        capacity_record(log, 150, 0.5)
        capacity_record(log, 190, 1.0)
        self.assertEqual(capacity_min(log, 200, 60), 0.5)
        self.assertEqual(capacity_min(log, 200, 20), 1.0)
        self.assertEqual(capacity_min(log, 400, 20), float("inf"))

    def test_need(self):
        self.assertEqual(need(5, 10), 0.5)

    def test_lane_value(self):
        self.assertEqual(lane_value(100, 1024, 0.0, 10), 102390)
        self.assertEqual(lane_value(100, 2048, 0.5, 0), 51200)

    def _items(self):
        a, b, c = b"a" * 32, b"b" * 32, b"c" * 32
        return a, b, c, [(a, 5.0, 1), (b, 1.0, 1), (c, 1.0, 9)]

    def test_release_order(self):
        a, b, c, items = self._items()
        self.assertEqual(lanes_to_release(items, 10, 0.15), [b, c])

    def test_release_tie_goes_to_farthest_in_any_input_order(self):
        a, b, c, _ = self._items()
        items = [(c, 1.0, 9), (b, 1.0, 1), (a, 5.0, 1)]
        self.assertEqual(lanes_to_release(items, 10, 0.15), [b, c])

    def test_release_tie_break_picks_single_lane(self):
        a, b, c, _ = self._items()
        for items in (
            [(b, 1.0, 1), (c, 1.0, 9), (a, 5.0, 1)],
            [(c, 1.0, 9), (b, 1.0, 1), (a, 5.0, 1)],
        ):
            self.assertEqual(lanes_to_release(items, 10, 0.25), [b])

    def test_release_none_when_enough(self):
        _, _, _, items = self._items()
        self.assertEqual(lanes_to_release(items, 10, 1.0), [])


if __name__ == "__main__":
    unittest.main()
