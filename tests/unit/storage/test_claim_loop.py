import logging
import sys
import unittest
from collections import deque
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.storage.workers import claim as claim_mod
from astreum.storage.workers.claim_flow import InFlight

A, B, C = b"\x0a" * 32, b"\x0b" * 32, b"\x0c" * 32
RATE = Fraction(1, 1000)


def _entry(storage_id=A):
    return (storage_id, b"slot", [b"\x00" * 32], 1, 0, 10)


class _Steps:
    """A stop event that runs one callable before each pass, then stops."""

    def __init__(self, steps):
        self.steps = steps
        self.calls = 0

    def is_set(self):
        index = self.calls
        self.calls += 1
        if index >= len(self.steps):
            return True
        self.steps[index]()
        return False

    def wait(self, timeout=None):
        return False


class TestClaimLoop(unittest.TestCase):
    def setUp(self):
        self.counter = 0
        self.node = SimpleNamespace(
            is_storage_provider=True,
            latest_block=None,
            new_record_ids=deque(),
            storage_claim_band=1,
            storage_claim_hash_rate=5.0,
            storage_claim_inclusion_delay=1,
            config=SimpleNamespace(storage_claim_fee_window=64, storage_claim_min_profit=0),
            logger=logging.getLogger("test_claim_loop"),
        )
        self.records = [A, B, C]
        self.fair = None
        self.step = MagicMock(return_value=[_entry()])
        self.send = MagicMock(side_effect=self._send)
        self.estimate = MagicMock(return_value=RATE)
        self.measure = MagicMock(return_value=7.0)
        self.capacity_min = None
        self.real_send = False
        self.scan = lambda node, batch=None: iter(self.records)

    def _send(self, node, head, entries, counter, rate):
        counter = self.counter if counter is None else counter
        return InFlight(counter, node.latest_block.height, entries, tx_bytes=100)

    def _head(self, height):
        tree = object()
        account = SimpleNamespace(data=tree)
        head = SimpleNamespace(
            height=height,
            tree=tree,
            accounts=SimpleNamespace(get_account=lambda address, node: account),
        )
        return head

    def _set_head(self, height):
        head = self._head(height)
        self.node.latest_block = head
        return head

    def _at(self, height, counter=None):
        def run():
            if counter is not None:
                self.counter = counter
            self._set_head(height)

        return run

    def _run(self, steps):
        self.node.communication_stop_event = _Steps(steps)
        patches = [
            patch.object(claim_mod, "_claim_step", self.step),
            patch.object(claim_mod, "_chain_counter", lambda node, block=None: self.counter),
            patch.object(claim_mod, "measure_hash_rate", self.measure),
            patch.object(claim_mod, "iter_records_in_cold_storage", self.scan),
            patch.object(claim_mod, "fair_interval", lambda head: self.fair),
            patch.object(claim_mod, "estimate_storage_rate", self.estimate),
            patch.object(claim_mod, "calculate_discount_rate", lambda *a, **k: 0.0),
            patch.object(claim_mod, "SLOT_DURATION_SECONDS", 0),
        ]
        if not self.real_send:
            patches.append(patch.object(claim_mod, "_send_batch", self.send))
        if self.capacity_min is not None:
            patches.append(patch.object(claim_mod, "capacity_min", lambda *a: self.capacity_min))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        claim_mod.claim_storage(self.node)

    def test_one_tx_in_flight(self):
        self._run([self._at(10), self._at(10), self._at(10)])
        self.assertEqual(self.send.call_count, 1)

    def test_next_batch_after_inclusion(self):
        self._run([self._at(10), self._at(11, counter=1)])
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.node.storage_claim_band, 2)
        self.assertEqual(self.node.storage_claim_inclusion_delay, 1)

    def test_lost_tx_resent_with_same_counter(self):
        self.node.storage_claim_band = 4
        entries = [_entry()]
        self.step.return_value = entries
        self._run([self._at(10), self._at(12)])
        self.assertEqual(self.send.call_count, 2)
        first, second = self.send.call_args_list
        self.assertEqual(second.args[2], first.args[2])
        self.assertIsNone(first.args[3])
        self.assertEqual(second.args[3], 0)
        self.assertEqual(self.node.storage_claim_band, 2)

    def test_new_record_ids_drained(self):
        self.node.new_record_ids.extend([A, B])
        self.records = []
        self.step.return_value = []
        self._run([self._at(10)])
        queue = self.step.call_args.args[4]
        self.assertEqual(queue.known, {A, B})
        self.assertEqual(len(self.node.new_record_ids), 0)

    def test_hash_rate_benchmark_once(self):
        self.node.storage_claim_hash_rate = None
        self.step.return_value = []
        self._run([self._at(10), self._at(10)])
        self.measure.assert_called_once()
        self.assertEqual(self.node.storage_claim_hash_rate, 7.0)

    def test_hash_rate_kept_when_set(self):
        self.step.return_value = []
        self._run([self._at(10)])
        self.measure.assert_not_called()
        self.assertEqual(self.node.storage_claim_hash_rate, 5.0)

    def test_release_when_need_over_capacity(self):
        self.fair = 10
        self.capacity_min = 0.1
        self.records = []
        seen = []

        def step(node, head, tree, ids, queue, held, rate, bytes_per_claim, should_stop):
            seen.append((set(held), set(queue.released)))
            if not held:
                held.update({A: (100, 0), B: (200, 0), C: (300, 0)})
            return []

        self.step.side_effect = step
        self._run([self._at(10), self._at(10)])
        self.assertEqual(seen[1], ({C}, {A, B}))

    def test_rate_estimated_once_per_head(self):
        self.step.return_value = []
        self._run([self._at(10)] + [lambda: None] * 2)
        self.estimate.assert_called_once()

    def test_no_rate_no_claims(self):
        self.estimate.side_effect = ValueError("no fees")
        self._run([self._at(10), self._at(10)])
        self.step.assert_not_called()

    def test_bytes_per_claim_from_last_send(self):
        entries = [_entry(A), _entry(B), _entry(C)]
        self.step.return_value = entries
        self.send.side_effect = lambda node, head, e, counter, rate: InFlight(
            self.counter if counter is None else counter, head.height, e, tx_bytes=900
        )
        self._run([self._at(10), self._at(11, counter=1)])
        self.assertEqual(self.step.call_count, 2)
        self.assertEqual(self.step.call_args_list[0].args[7], 0)
        self.assertEqual(self.step.call_args_list[1].args[7], 300)

    def test_failed_account_read_is_logged_and_retried(self):
        def broken():
            head = self._head(10)
            head.accounts = SimpleNamespace(get_account=MagicMock(side_effect=RuntimeError("read failed")))
            self.node.latest_block = head

        with self.assertLogs("test_claim_loop", level="ERROR") as logs:
            self._run([broken, self._at(10)])
        self.assertIn("read failed", "\n".join(logs.output))
        self.estimate.assert_called_once()
        self.step.assert_called_once()
        self.assertEqual(self.send.call_count, 1)

    def test_send_height_is_the_head_at_the_send(self):
        self.real_send = True
        self.node.storage_claim_band = 4
        self.node.storage_secret_key = object()
        self.node.config.storage_claim_min_profit = -(10**12)

        def step(*args):
            self._set_head(12)
            return [_entry()]

        self.step.side_effect = step
        patches = [
            patch.object(claim_mod, "_build_multi_claim_tx", lambda *a: SimpleNamespace(hash=b"h", expr=lambda: None)),
            patch.object(claim_mod, "_tree_bytes", lambda expr: 100),
            patch.object(claim_mod, "storage_fee_at_rate", lambda *a: 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with patch("astreum.consensus.transaction.send.send_transaction") as send_transaction:
            self._run([self._at(10), lambda: None])
        self.assertEqual(send_transaction.call_count, 1)
        self.assertEqual(self.node.storage_claim_band, 4)

    def test_startup_scan_queues_every_record_in_one_pass(self):
        self.records = [bytes([i]) * 32 for i in range(1, 101)]
        self.step.return_value = []
        self._run([self._at(10)])
        queue = self.step.call_args.args[4]
        self.assertEqual(queue.known, set(self.records))

    def test_finished_scan_is_not_read_again(self):
        starts = []

        def scan(node, batch=None):
            starts.append(batch)
            return iter([A, B])

        self.scan = scan
        self.step.return_value = []
        self._run([self._at(10), self._at(10), self._at(10)])
        self.assertEqual(starts, [1])

    def test_scan_error_is_logged_and_the_scan_starts_again(self):
        def failing():
            yield A
            raise OSError("scan failed")

        scans = iter([failing(), iter([A, B, C])])
        self.scan = lambda node, batch=None: next(scans)
        known = []
        self.step.side_effect = lambda node, head, tree, ids, queue, *rest: known.append(set(queue.known)) or []
        with self.assertLogs("test_claim_loop", level="ERROR") as logs:
            self._run([self._at(10), self._at(10)])
        self.assertIn("scan failed", "\n".join(logs.output))
        self.assertEqual(known, [{A}, {A, B, C}])

    def test_zero_fair_interval_does_not_fail_the_pass(self):
        self.fair = 0
        self.capacity_min = 0.1
        self.records = []

        def step(node, head, tree, ids, queue, held, rate, bytes_per_claim, should_stop):
            held.update({A: (100, 0), B: (200, 0)})
            return []

        self.step.side_effect = step
        with self.assertNoLogs("test_claim_loop", level="ERROR"):
            self._run([self._at(10), self._at(10)])

    def test_released_ids_are_dropped_from_pending(self):
        entries = [_entry(A), _entry(B)]
        calls = []

        def step(node, head, tree, ids, queue, held, rate, bytes_per_claim, should_stop):
            calls.append(ids)
            if len(calls) == 1:
                queue.released.add(B)
                return entries
            return []

        self.step.side_effect = step
        with patch.object(claim_mod, "_split_claims", lambda e: [e[:1], e[1:]]):
            self._run([self._at(10), self._at(11, counter=1)])
        self.assertEqual(len(calls), 2)
        self.assertEqual([c.args[2] for c in self.send.call_args_list], [[entries[0]]])

    def test_failed_step_puts_popped_ids_back(self):
        heaps = []

        def step(node, head, tree, ids, queue, held, rate, bytes_per_claim, should_stop):
            heaps.append(sorted(queue.heap))
            if len(heaps) == 1:
                self.assertEqual(ids, [A])
                raise RuntimeError("step failed")
            return []

        self.step.side_effect = step
        with self.assertLogs("test_claim_loop", level="ERROR") as logs:
            self._run([self._at(10), self._at(10)])
        self.assertIn("step failed", "\n".join(logs.output))
        self.assertEqual(len(heaps), 2)
        self.assertIn((11, A), heaps[1])
        self.assertEqual(heaps[1].count((11, A)), 1)


if __name__ == "__main__":
    unittest.main()
