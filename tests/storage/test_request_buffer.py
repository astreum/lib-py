import math
import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.storage_request.model import StorageRequest, max_get_entries
from astreum.expression import RESOLUTION_LIST, RESOLUTION_SINGLE
from astreum.storage.requests import claim_expr_req, has_expr_req
from astreum.storage.workers.requests import (
    flush_storage_requests,
    init_request_buffer,
    queue_storage_get,
    request_storage,
)

PEER_KEY = b"\x11" * 32
SHARED = b"\x01" * 32
ENQUEUE = "astreum.communication.outgoing_queue.enqueue_outgoing"


def _ids(n: int, start: int = 0) -> list[bytes]:
    return [(start + i + 1).to_bytes(32, "big") for i in range(n)]


def _peer(port: int = 9000, difficulty: int = 1, key: bytes = PEER_KEY):
    return SimpleNamespace(
        address=("127.0.0.1", port),
        public_key_bytes=key,
        shared_key_bytes=SHARED,
        difficulty=difficulty,
    )


def _node(peer_for=None, flush_interval: float = 0.05):
    """A node whose closest peer is ``peer_for(expr_id)`` (default: one peer)."""
    default = _peer()
    node = SimpleNamespace(
        config={"storage_request_flush_interval": flush_interval},
        logger=MagicMock(),
        storage_index={},
        storage_providers=[],
        storage_public_key_bytes=b"\x00" * 32,
        relay_secret_key=X25519PrivateKey.generate(),
        peer_route=SimpleNamespace(
            closest_peer_for_hash=peer_for or (lambda h: default)
        ),
        outgoing_queue=queue.Queue(),
        expr_requests={},
        expr_requests_lock=threading.RLock(),
        communication_stop_event=threading.Event(),
    )
    init_request_buffer(node)
    return node


def _drain(node, shared=SHARED):
    """Decode every datagram in the outgoing queue: [(address, entries)]."""
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        message = Message.from_bytes(payload[8:])
        message.decrypt(shared)
        assert message.topic == MessageTopic.STORAGE_REQUEST
        out.append((address, StorageRequest.from_bytes(message.content).entries))
    return out


class TestQueue(unittest.TestCase):
    def test_queue_then_flush_sends_one_get_with_all_entries(self):
        node = _node()
        ids = _ids(5)
        for h in ids:
            self.assertTrue(queue_storage_get(node, h, RESOLUTION_SINGLE))
        self.assertTrue(node.outgoing_queue.empty())  # nothing sent until flush
        self.assertEqual(flush_storage_requests(node), 1)
        sent = _drain(node)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ("127.0.0.1", 9000))
        self.assertEqual(sent[0][1], [(h, RESOLUTION_SINGLE) for h in ids])
        for h in ids:
            self.assertTrue(has_expr_req(node, h))

    def test_entries_keep_their_own_resolution(self):
        node = _node()
        a, b = _ids(2)
        queue_storage_get(node, a, RESOLUTION_SINGLE)
        queue_storage_get(node, b, RESOLUTION_LIST)
        flush_storage_requests(node)
        self.assertEqual(_drain(node)[0][1], [(a, RESOLUTION_SINGLE), (b, RESOLUTION_LIST)])

    def test_n_gets_produce_ceil_n_over_34_datagrams(self):
        node = _node()
        per = max_get_entries(1232)
        self.assertEqual(per, 34)
        n = 2 * per + 5
        for h in _ids(n):
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        self.assertEqual(flush_storage_requests(node), math.ceil(n / per))
        sent = _drain(node)
        self.assertEqual([len(e) for _, e in sent], [per, per, 5])

    def test_different_destinations_get_separate_datagrams(self):
        peers = {0: _peer(9001, key=b"\x01" * 32), 1: _peer(9002, key=b"\x02" * 32)}
        node = _node(peer_for=lambda h: peers[h[-1] % 2])
        ids = _ids(6)
        for h in ids:
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        self.assertEqual(flush_storage_requests(node), 2)
        sent = _drain(node)
        self.assertEqual({a for a, _ in sent}, {("127.0.0.1", 9001), ("127.0.0.1", 9002)})
        self.assertEqual(sum(len(e) for _, e in sent), 6)

    def test_already_buffered_hash_is_added_once(self):
        node = _node()
        h = _ids(1)[0]
        self.assertTrue(queue_storage_get(node, h, RESOLUTION_SINGLE))
        self.assertTrue(queue_storage_get(node, h, RESOLUTION_SINGLE))
        flush_storage_requests(node)
        self.assertEqual(_drain(node)[0][1], [(h, RESOLUTION_SINGLE)])

    def test_live_request_is_not_buffered(self):
        node = _node()
        h = _ids(1)[0]
        claim_expr_req(node, h, RESOLUTION_SINGLE)
        self.assertTrue(queue_storage_get(node, h, RESOLUTION_SINGLE))
        self.assertEqual(flush_storage_requests(node), 0)
        self.assertTrue(node.outgoing_queue.empty())

    def test_request_claimed_between_queue_and_flush_is_dropped(self):
        node = _node()
        a, b = _ids(2)
        queue_storage_get(node, a, RESOLUTION_SINGLE)
        queue_storage_get(node, b, RESOLUTION_SINGLE)
        claim_expr_req(node, a, RESOLUTION_SINGLE)  # someone else got there first
        flush_storage_requests(node)
        self.assertEqual(_drain(node)[0][1], [(b, RESOLUTION_SINGLE)])

    def test_no_destination_returns_false_and_buffers_nothing(self):
        node = _node(peer_for=lambda h: None)
        self.assertFalse(queue_storage_get(node, _ids(1)[0], RESOLUTION_SINGLE))
        self.assertEqual(flush_storage_requests(node), 0)
        self.assertEqual(node.storage_request_buffer, {})

    def test_unknown_provider_id_returns_false(self):
        node = _node()
        h = _ids(1)[0]
        node.storage_index[h] = 99
        self.assertFalse(queue_storage_get(node, h, RESOLUTION_SINGLE))

    def test_indexed_provider_destination_uses_difficulty_one(self):
        node = _node()
        h = _ids(1)[0]
        provider = X25519PrivateKey.generate()
        relay = provider.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        payload = b"\x22" * 32 + relay + bytes([10, 0, 0, 7]) + (5000).to_bytes(2, "big")
        node.storage_providers.append(payload)
        node.storage_index[h] = 0
        queue_storage_get(node, h, RESOLUTION_SINGLE)
        shared = provider.exchange(node.relay_secret_key.public_key())
        with patch(ENQUEUE, return_value=True) as enq:
            flush_storage_requests(node)
        self.assertEqual(enq.call_args.args[1], ("10.0.0.7", 5000))
        self.assertEqual(enq.call_args.kwargs["difficulty"], 1)
        message = enq.call_args.kwargs["message"]
        message.decrypt(shared)
        self.assertEqual(StorageRequest.from_bytes(message.content).entries, [(h, RESOLUTION_SINGLE)])


class TestPreclaimed(unittest.TestCase):
    def _contact(self):
        provider = X25519PrivateKey.generate()
        relay = provider.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return provider, (relay, "10.1.1.1", 6000)

    def test_contact_overrides_routing_and_claim_is_not_repeated(self):
        node = _node()
        a, b = _ids(2)
        provider, contact = self._contact()
        claim_expr_req(node, a, RESOLUTION_LIST)
        claim_expr_req(node, b, RESOLUTION_SINGLE)
        self.assertTrue(queue_storage_get(node, a, RESOLUTION_LIST, contact=contact, preclaimed=True))
        self.assertTrue(queue_storage_get(node, b, RESOLUTION_SINGLE, contact=contact, preclaimed=True))
        shared = provider.exchange(node.relay_secret_key.public_key())
        flush_storage_requests(node)
        sent = _drain(node, shared)
        self.assertEqual(sent, [(("10.1.1.1", 6000), [(a, RESOLUTION_LIST), (b, RESOLUTION_SINGLE)])])

    def test_preclaimed_entry_whose_request_is_gone_is_dropped(self):
        node = _node()
        a = _ids(1)[0]
        _, contact = self._contact()
        queue_storage_get(node, a, RESOLUTION_SINGLE, contact=contact, preclaimed=True)
        self.assertEqual(flush_storage_requests(node), 0)

    def test_bad_contact_returns_false(self):
        node = _node()
        self.assertFalse(
            queue_storage_get(node, _ids(1)[0], RESOLUTION_SINGLE,
                              contact=(b"short", "1.1.1.1", 1), preclaimed=True)
        )


class TestFlush(unittest.TestCase):
    def test_failed_enqueue_releases_claims(self):
        node = _node()
        ids = _ids(3)
        for h in ids:
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        with patch(ENQUEUE, return_value=False):
            self.assertEqual(flush_storage_requests(node), 0)
        for h in ids:
            self.assertFalse(has_expr_req(node, h))

    def test_raising_enqueue_releases_claims(self):
        node = _node()
        h = _ids(1)[0]
        queue_storage_get(node, h, RESOLUTION_SINGLE)
        with patch(ENQUEUE, side_effect=RuntimeError("boom")):
            flush_storage_requests(node)
        self.assertFalse(has_expr_req(node, h))

    def test_one_failing_chunk_does_not_stop_the_next(self):
        node = _node()
        per = max_get_entries(1232)
        ids = _ids(per + 1)
        for h in ids:
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        with patch(ENQUEUE, side_effect=[False, True]):
            self.assertEqual(flush_storage_requests(node), 1)
        self.assertFalse(has_expr_req(node, ids[0]))
        self.assertTrue(has_expr_req(node, ids[-1]))

    def test_lowest_difficulty_destination_goes_first(self):
        peers = {
            0: _peer(9001, difficulty=12, key=b"\x01" * 32),
            1: _peer(9002, difficulty=1, key=b"\x02" * 32),
            2: _peer(9003, difficulty=5, key=b"\x03" * 32),
        }
        node = _node(peer_for=lambda h: peers[h[-1] % 3])
        for h in _ids(6):
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        order = []
        with patch(ENQUEUE, side_effect=lambda n, a, message, difficulty=1: order.append(difficulty) or True):
            flush_storage_requests(node)
        self.assertEqual(order, [1, 5, 12])

    def test_flush_empties_the_buffer(self):
        node = _node()
        queue_storage_get(node, _ids(1)[0], RESOLUTION_SINGLE)
        flush_storage_requests(node)
        self.assertEqual(flush_storage_requests(node), 0)

    def test_filling_a_datagram_sets_the_flush_event(self):
        node = _node()
        per = max_get_entries(1232)
        ids = _ids(per)
        for h in ids[:-1]:
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        self.assertFalse(node.storage_request_flush_event.is_set())
        queue_storage_get(node, ids[-1], RESOLUTION_SINGLE)
        self.assertTrue(node.storage_request_flush_event.is_set())


class TestThread(unittest.TestCase):
    def _start(self, node):
        thread = threading.Thread(target=request_storage, args=(node,), daemon=True)
        thread.start()
        self.addCleanup(lambda: (node.communication_stop_event.set(), thread.join(2)))
        return thread

    def _wait_sent(self, node, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not node.outgoing_queue.empty():
                return True
            time.sleep(0.01)
        return False

    def test_thread_flushes_on_the_interval(self):
        node = _node(flush_interval=0.05)
        self._start(node)
        for h in _ids(3):
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        self.assertTrue(self._wait_sent(node))
        self.assertEqual(len(_drain(node)[0][1]), 3)

    def test_full_datagram_flushes_before_the_interval(self):
        node = _node(flush_interval=30.0)
        self._start(node)
        for h in _ids(max_get_entries(1232)):
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        self.assertTrue(self._wait_sent(node))

    def test_stop_event_ends_the_thread(self):
        node = _node(flush_interval=0.05)
        thread = self._start(node)
        node.communication_stop_event.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_thread_survives_a_failing_flush(self):
        node = _node(flush_interval=0.02)
        with patch(
            "astreum.storage.workers.requests.flush_storage_requests",
            side_effect=[RuntimeError("x")] + [0] * 200,
        ) as flush:
            thread = self._start(node)
            time.sleep(0.3)
            self.assertTrue(thread.is_alive())
            self.assertGreater(flush.call_count, 1)  # kept going after the error


if __name__ == "__main__":
    unittest.main()
