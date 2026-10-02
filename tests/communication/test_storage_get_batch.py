import queue
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.communication.message_pow import (
    MAX_INLINE_MESSAGE_BYTES,
    MESSAGE_FRAMING_BYTES,
)
from astreum.communication.storage_request import handle as req_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest, max_get_entries
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.expression import (
    RESOLUTION_FULL,
    RESOLUTION_RECORD,
    RESOLUTION_SINGLE,
    bytes_,
)
from astreum.utils.config import DEFAULT_STORAGE_GET_BATCH_MAX_BYTES, config_setup

PROVIDER = b"P" * 70


def _ids(n: int, start: int = 0) -> list[bytes]:
    return [(start + i + 1).to_bytes(32, "big") for i in range(n)]


class TestGetCodec(unittest.TestCase):
    def _req(self, n, resolution=RESOLUTION_SINGLE):
        return StorageRequest(
            StorageRequestCode.STORAGE_GET,
            entries=[(e, resolution) for e in _ids(n)],
        )

    def test_round_trip(self):
        entries = [(e, i % 4) for i, e in enumerate(_ids(5))]
        decoded = StorageRequest.from_bytes(
            StorageRequest(StorageRequestCode.STORAGE_GET, entries=entries).to_bytes()
        )
        self.assertEqual(decoded.code, StorageRequestCode.STORAGE_GET)
        self.assertEqual(decoded.entries, entries)

    def test_single_entry_is_three_bytes_plus_entry(self):
        self.assertEqual(len(self._req(1).to_bytes()), 3 + 33)

    def test_layout(self):
        body = self._req(2, RESOLUTION_FULL).to_bytes()
        self.assertEqual(body[0], StorageRequestCode.STORAGE_GET.value)
        self.assertEqual(int.from_bytes(body[1:3], "big"), 2)
        self.assertEqual(body[3 + 32], RESOLUTION_FULL)

    def test_encode_rejects_empty_and_bad_id(self):
        with self.assertRaises(ValueError):
            StorageRequest(StorageRequestCode.STORAGE_GET, entries=[]).to_bytes()
        with self.assertRaises(ValueError):
            StorageRequest(StorageRequestCode.STORAGE_GET, entries=[(b"short", 0)]).to_bytes()

    def test_decode_rejects_count_zero(self):
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(bytes([StorageRequestCode.STORAGE_GET]) + b"\x00\x00")

    def test_decode_rejects_length_mismatch(self):
        good = self._req(2).to_bytes()
        for bad in (good[:-1], good + b"\x00"):
            with self.assertRaises(ValueError):
                StorageRequest.from_bytes(bad)

    def test_decode_rejects_old_single_get_format(self):
        old = bytes([StorageRequestCode.STORAGE_GET]) + b"\x07" * 32 + bytes([1])
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(old)

    def test_decode_rejects_oversized(self):
        raw = bytes([StorageRequestCode.STORAGE_GET]) + b"\x00" * MAX_INLINE_MESSAGE_BYTES
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw)

    def test_decode_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(b"")

    def test_default_budget_gives_34_entries(self):
        self.assertEqual(DEFAULT_STORAGE_GET_BATCH_MAX_BYTES, 1232)
        self.assertEqual(max_get_entries(1232), 34)

    def test_full_batch_fits_budget(self):
        body = self._req(34).to_bytes()
        self.assertLessEqual(len(body) + MESSAGE_FRAMING_BYTES, 1232)
        self.assertGreater(len(self._req(35).to_bytes()) + MESSAGE_FRAMING_BYTES, 1232)


class TestGetConfig(unittest.TestCase):
    def test_defaults(self):
        config = config_setup({"default_seed": None})
        self.assertEqual(config["storage_get_batch_max_bytes"], 1232)
        self.assertEqual(config["storage_request_flush_interval"], 0.25)

    def test_overrides(self):
        config = config_setup(
            {
                "default_seed": None,
                "storage_get_batch_max_bytes": 2000,
                "storage_request_flush_interval": 1,
            }
        )
        self.assertEqual(config["storage_get_batch_max_bytes"], 2000)
        self.assertEqual(config["storage_request_flush_interval"], 1.0)

    def test_invalid_batch_bytes_rejected(self):
        for bad in (True, "abc", 100, 70000):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"default_seed": None, "storage_get_batch_max_bytes": bad})

    def test_invalid_flush_interval_rejected(self):
        for bad in (True, "abc", 0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"default_seed": None, "storage_request_flush_interval": bad})


class TestGetServer(unittest.TestCase):
    """handle_storage_request for a batched STORAGE_GET."""

    def setUp(self):
        self.found = bytes_(b"found")
        self.found_b = bytes_(b"found-b")
        self.local = {self.found.hash(): self.found, self.found_b.hash(): self.found_b}
        self.redirect_id = b"\xaa" * 32
        self.unknown_id = b"\xbb" * 32

    def _node(self):
        return SimpleNamespace(
            logger=MagicMock(),
            storage_public_key_bytes=b"\x00" * 32,
            storage_index={self.redirect_id: 0},
            storage_providers=[PROVIDER],
            outgoing_queue=queue.Queue(),
            config={},
            peer_route=SimpleNamespace(closest_peer_for_hash=lambda h: None),
        )

    def _peer(self):
        return SimpleNamespace(
            address=("127.0.0.1", 1), shared_key_bytes=b"\x01" * 32, difficulty=1, metrics={}
        )

    def _serve(self, entries, *, local=None, requires_channel=None, node=None, record_blob=None):
        node = node or self._node()
        request = StorageRequest(StorageRequestCode.STORAGE_GET, entries=entries)
        message = SimpleNamespace(content=request.to_bytes())
        sent, payment = [], []
        local = self.local if local is None else local

        def fake_local(n, h):
            value = local.get(h)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(req_handle, "get_expr_from_local_storage", side_effect=fake_local), \
             patch.object(req_handle, "_requires_storage_channel",
                          side_effect=requires_channel or (lambda n, p, size: False)), \
             patch.object(req_handle, "_queue_storage_payment_required",
                          side_effect=lambda n, p, h, size: payment.append(h)), \
             patch.object(req_handle, "get_record_from_cold_storage", return_value=record_blob), \
             patch.object(req_handle, "enqueue_outgoing",
                          side_effect=lambda n, a, message, difficulty=1: sent.append(message) or True), \
             patch.object(req_handle, "increment_peer_metric"):
            result = req_handle.handle_storage_request(node, self._peer(), message)
        responses = [StorageResponse.from_bytes(m.content) for m in sent]
        return result, responses, payment

    def test_mixed_found_redirect_unknown(self):
        entries = [
            (self.found.hash(), RESOLUTION_SINGLE),
            (self.redirect_id, RESOLUTION_SINGLE),
            (self.unknown_id, RESOLUTION_SINGLE),
        ]
        result, responses, _ = self._serve(entries)
        self.assertEqual(result, (True, None))
        by_id = {r.expr_id: r.code for r in responses}
        self.assertEqual(by_id[self.found.hash()], StorageResponseCode.STORAGE_FOUND)
        self.assertEqual(by_id[self.redirect_id], StorageResponseCode.STORAGE_PROVIDER)
        self.assertNotIn(self.unknown_id, by_id)
        self.assertEqual(len(responses), 2)

    def test_duplicate_entries_served_once(self):
        h = self.found.hash()
        result, responses, _ = self._serve(
            [(h, RESOLUTION_SINGLE), (h, RESOLUTION_SINGLE), (h, RESOLUTION_SINGLE)]
        )
        self.assertEqual(result, (True, None))
        self.assertEqual(len(responses), 1)

    def test_fair_use_runs_per_entry_and_does_not_stop_the_rest(self):
        calls = []

        def requires(n, p, size):
            calls.append(size)
            return len(calls) == 1  # only the first entry hits the limit

        entries = [(self.found.hash(), RESOLUTION_SINGLE), (self.found_b.hash(), RESOLUTION_SINGLE)]
        result, responses, payment = self._serve(entries, requires_channel=requires)
        self.assertEqual(result, (True, None))
        self.assertEqual(len(calls), 2)
        self.assertEqual(payment, [self.found.hash()])
        self.assertEqual([r.expr_id for r in responses], [self.found_b.hash()])

    def test_raising_entry_does_not_affect_the_others(self):
        local = dict(self.local)
        local[self.found.hash()] = RuntimeError("disk")
        entries = [(self.found.hash(), RESOLUTION_SINGLE), (self.found_b.hash(), RESOLUTION_SINGLE)]
        result, responses, _ = self._serve(entries, local=local)
        self.assertEqual(result, (True, None))
        self.assertEqual([r.expr_id for r in responses], [self.found_b.hash()])

    def test_failing_entry_does_not_affect_the_others(self):
        # found_b is requested as a record but has no records-table entry
        entries = [(self.found_b.hash(), RESOLUTION_RECORD), (self.found.hash(), RESOLUTION_SINGLE)]
        result, responses, _ = self._serve(entries, record_blob=None)
        self.assertEqual(result, (True, None))
        self.assertEqual([r.expr_id for r in responses], [self.found.hash()])

    def test_every_entry_failing_reports_failure(self):
        entries = [(self.found_b.hash(), RESOLUTION_RECORD)]
        result, responses, _ = self._serve(entries, record_blob=None)
        self.assertEqual(result, (False, "not a record"))
        self.assertEqual(responses, [])

    def test_each_entry_uses_its_own_resolution(self):
        seen = []
        with patch.object(req_handle, "_collect_for_resolution",
                          side_effect=lambda expr, desired: seen.append(desired) or [expr]):
            self._serve(
                [(self.found.hash(), RESOLUTION_SINGLE), (self.found_b.hash(), RESOLUTION_FULL)]
            )
        self.assertEqual(seen, [RESOLUTION_SINGLE, RESOLUTION_FULL])


if __name__ == "__main__":
    unittest.main()
