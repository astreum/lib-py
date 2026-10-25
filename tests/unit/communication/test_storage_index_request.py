import queue
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.route import Route
from astreum.communication.storage_request import handle as put_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.communication.self_lookup import SelfLookup, SelfLookupTimer
from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.workers.advertisements import (
    _index_handoff_step,
    _send_branch_index_requests,
    _send_index_requests,
    _serve_one_index_request,
)
from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES
from astreum.utils.config import NodeConfig

SELF_KEY = b"\x00" * 32
REQUESTER_KEY = b"\xff" * 32
PROVIDER_A = b"A" * 70
PROVIDER_B = b"B" * 70
SHARED = b"\x01" * 32


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


def _peer(key: bytes, port: int):
    return SimpleNamespace(
        address=("127.0.0.1", port),
        public_key_bytes=key,
        shared_key_bytes=SHARED,
        difficulty=1,
    )


def _node(peers=(), synced=True):
    return SimpleNamespace(
        config=NodeConfig(
            storage_public_key_bytes=SELF_KEY,
            storage_put_batch_max_bytes=DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES,
            self_lookup_rounds=8,
            self_lookup_interval=900,
            peer_timeout_interval=10,
        ),
        peer_route=Route(SELF_KEY),
        storage_public_key_bytes=SELF_KEY,
        logger=_silent_logger(),
        outgoing_queue=queue.Queue(),
        storage_index={},
        storage_providers=[PROVIDER_A, PROVIDER_B],
        peers={peer.public_key_bytes: peer for peer in peers},
        peers_lock=threading.RLock(),
        index_requests=[],
        index_requests_lock=threading.Lock(),
        put_requests=[],
        put_requests_lock=threading.Lock(),
        index_requested=False,
        latest_block=object() if synced else None,
    )


def _id(prefix: int, n: int = 0) -> bytes:
    """A 32-byte id. Prefix 0xFF is nearer to REQUESTER_KEY, 0x00 to SELF_KEY."""
    return bytes([prefix]) + n.to_bytes(31, "big")


def _sent(node):
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        message = Message.from_bytes(payload[8:])
        message.decrypt(SHARED)
        assert message.topic == MessageTopic.STORAGE_REQUEST
        out.append((address, StorageRequest.from_bytes(message.content)))
    return out


class TestCodec(unittest.TestCase):
    def test_encode_decode_round_trip(self):
        req = StorageRequest(code=StorageRequestCode.STORAGE_INDEX_REQUEST)
        raw = req.to_bytes()
        self.assertEqual(raw, bytes([2]))
        decoded = StorageRequest.from_bytes(raw)
        self.assertEqual(decoded.code, StorageRequestCode.STORAGE_INDEX_REQUEST)

    def test_rejects_extra_bytes(self):
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(bytes([2, 0]))


class TestHandler(unittest.TestCase):
    def _handle(self, node, sender):
        message = SimpleNamespace(
            content=StorageRequest(code=StorageRequestCode.STORAGE_INDEX_REQUEST).to_bytes()
        )
        return put_handle.handle_storage_request(node, sender, message)

    def test_handler_queues_once(self):
        node = _node()
        sender = _peer(REQUESTER_KEY, 9001)
        self.assertEqual(self._handle(node, sender), (True, None))
        self.assertEqual(self._handle(node, sender), (True, None))
        self.assertEqual(node.index_requests, [REQUESTER_KEY])

    def test_handler_does_no_work(self):
        node = _node()
        node.storage_index[_id(0xFF)] = (0,)
        self._handle(node, _peer(REQUESTER_KEY, 9001))
        self.assertTrue(node.outgoing_queue.empty())


class TestResponder(unittest.TestCase):
    def setUp(self):
        self.requester = _peer(REQUESTER_KEY, 9001)
        self.node = _node(peers=[self.requester])
        self.node.index_requests.append(REQUESTER_KEY)

    def test_selects_nearer_entries(self):
        near = [_id(0xFF, 1), _id(0xFF, 2)]
        far = [_id(0x00, 1), _id(0x00, 2)]
        for expr_id in near + far:
            self.node.storage_index[expr_id] = (0,)
        self.assertTrue(_serve_one_index_request(self.node))
        sent = _sent(self.node)
        ids = [e[0] for _, req in sent for e in req.entries]
        self.assertEqual(sorted(ids), sorted(near))

    def test_entries_use_single_resolution(self):
        self.node.storage_index[_id(0xFF)] = (0,)
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual(req.code, StorageRequestCode.STORAGE_PUT)
        self.assertEqual(req.entries, [(_id(0xFF), RESOLUTION_SINGLE)])

    def test_keeps_provider(self):
        self.node.storage_index[_id(0xFF, 1)] = (1,)
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual(req.data, PROVIDER_B)

    def test_groups_by_provider(self):
        self.node.storage_index[_id(0xFF, 1)] = (0,)
        self.node.storage_index[_id(0xFF, 2)] = (1,)
        self.node.storage_index[_id(0xFF, 3)] = (0,)
        _serve_one_index_request(self.node)
        sent = {req.data: [e[0] for e in req.entries] for _, req in _sent(self.node)}
        self.assertEqual(sorted(sent), sorted([PROVIDER_A, PROVIDER_B]))
        self.assertEqual(sorted(sent[PROVIDER_A]), [_id(0xFF, 1), _id(0xFF, 3)])
        self.assertEqual(sent[PROVIDER_B], [_id(0xFF, 2)])

    def test_keeps_own_entries(self):
        self.node.storage_index[_id(0xFF, 1)] = (0,)
        self.node.storage_index[_id(0x00, 1)] = (1,)
        before = dict(self.node.storage_index)
        _serve_one_index_request(self.node)
        self.assertEqual(self.node.storage_index, before)

    def test_unknown_peer_skipped(self):
        self.node.peers.clear()
        self.node.storage_index[_id(0xFF)] = (0,)
        self.assertTrue(_serve_one_index_request(self.node))
        self.assertTrue(self.node.outgoing_queue.empty())
        self.assertEqual(self.node.index_requests, [])

    def test_entry_with_two_providers_sent_in_one_message(self):
        expr_id = _id(0xFF, 1)
        self.node.storage_index[expr_id] = (0, 1)
        _serve_one_index_request(self.node)
        (_, request), = _sent(self.node)
        self.assertEqual(request.data, PROVIDER_A + PROVIDER_B)
        self.assertEqual([e[0] for e in request.entries], [expr_id])

    def test_entries_grouped_by_provider_set(self):
        both, only_a = _id(0xFF, 1), _id(0xFF, 2)
        self.node.storage_index[both] = (0, 1)
        self.node.storage_index[only_a] = (0,)
        _serve_one_index_request(self.node)
        by_data = {r.data: [e[0] for e in r.entries] for _, r in _sent(self.node)}
        self.assertEqual(by_data, {PROVIDER_A + PROVIDER_B: [both], PROVIDER_A: [only_a]})

    def test_unknown_provider_skipped(self):
        self.node.storage_index[_id(0xFF)] = (7,)
        _serve_one_index_request(self.node)
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_empty_queue_returns_false(self):
        self.node.index_requests.clear()
        self.assertFalse(_serve_one_index_request(self.node))

    def test_takes_one_request_per_call(self):
        other = _peer(b"\xfe" * 32, 9002)
        self.node.peers[other.public_key_bytes] = other
        self.node.index_requests.append(other.public_key_bytes)
        self.assertTrue(_serve_one_index_request(self.node))
        self.assertEqual(self.node.index_requests, [other.public_key_bytes])

    def _route_peer(self, key, port):
        peer = _peer(key, port)
        self.node.peer_route.add_peer(key, peer)
        return peer

    def test_sends_to_second_nearest(self):
        expr_id = _id(0xFF, 1)
        self._route_peer(_id(0xFF, 2), 9002)  # nearer to the key than the requester
        self.node.storage_index[expr_id] = (0,)
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual([e[0] for e in req.entries], [expr_id])

    def test_skips_third_nearest_requester(self):
        expr_id = _id(0x00, 1)
        self._route_peer(_id(0x00, 2), 9002)  # nearer than the requester, and so is self
        self.node.storage_index[expr_id] = (0,)
        _serve_one_index_request(self.node)
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_listed_provider_counts(self):
        expr_id = _id(0x00, 1)  # self and PROVIDER_A (key 0x41..) are nearer than the requester
        self.node.storage_index[expr_id] = (0,)
        _serve_one_index_request(self.node)
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_listed_provider_requester_gets_entry(self):
        requester_key = b"A" * 32  # the key of PROVIDER_A
        peer = _peer(requester_key, 9003)
        self.node.peers[requester_key] = peer
        self.node.index_requests[:] = [requester_key]
        expr_id = b"\x40" + b"\x00" * 31
        self.node.storage_index[expr_id] = (0, 1)
        _serve_one_index_request(self.node)
        (addr, req), = _sent(self.node)
        self.assertEqual(addr, peer.address)
        self.assertEqual([e[0] for e in req.entries], [expr_id])

    def test_unknown_provider_id_not_counted(self):
        expr_id = _id(0xFF, 1)
        self.node.storage_index[expr_id] = (7, 0)
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual(req.data, PROVIDER_A)


class TestRequester(unittest.TestCase):
    def test_waits_for_latest_block(self):
        node = _node(peers=[_peer(REQUESTER_KEY, 9001)], synced=False)
        self.assertFalse(_send_index_requests(node))
        self.assertTrue(node.outgoing_queue.empty())
        self.assertFalse(node.index_requested)

    def test_waits_for_a_peer(self):
        node = _node()
        self.assertFalse(_send_index_requests(node))
        self.assertFalse(node.index_requested)

    def test_sends_to_every_peer_once(self):
        a, b = _peer(REQUESTER_KEY, 9001), _peer(b"\x80" * 32, 9002)
        node = _node(peers=[a, b])
        self.assertTrue(_send_index_requests(node))
        sent = _sent(node)
        self.assertEqual(sorted(addr for addr, _ in sent), [a.address, b.address])
        self.assertTrue(
            all(req.code == StorageRequestCode.STORAGE_INDEX_REQUEST for _, req in sent)
        )
        self.assertFalse(_send_index_requests(node))
        self.assertTrue(node.outgoing_queue.empty())


class TestReplyUsesTrieCheck(unittest.TestCase):
    def test_reply_id_not_in_trie_is_not_indexed(self):
        """The requester handles a reply as a normal STORAGE_PUT."""
        responder = _node(peers=[_peer(REQUESTER_KEY, 9001)])
        responder.index_requests.append(REQUESTER_KEY)
        responder.storage_index[_id(0xFF)] = (0,)
        _serve_one_index_request(responder)
        (_, req), = _sent(responder)

        requester = _node()
        requester.config.storage_public_key_bytes = REQUESTER_KEY
        requester.peer_route = SimpleNamespace(closest_peer_for_hash=lambda h: None)
        sender = _peer(SELF_KEY, 9100)
        message = SimpleNamespace(content=req.to_bytes())
        with patch(
            "astreum.storage.admission.is_expr_in_latest_block", return_value=False
        ), patch(
            "astreum.storage.admission.get_latest_storage_account", return_value=None
        ):
            put_handle.handle_storage_request(requester, sender, message)
            queued_peer, queued_request = requester.put_requests.pop(0)
            put_handle.handle_put(requester, queued_peer, queued_request)
        self.assertEqual(requester.storage_index, {})


def _open_walk():
    """A walk with one round sent and a deadline far in the future."""
    return SelfLookup(rounds=1, distance=1, deadline=time.monotonic() + 1000.0)


class TestStep(unittest.TestCase):
    def test_step_reports_remaining_requests(self):
        node = _node(peers=[_peer(REQUESTER_KEY, 9001)], synced=False)
        node.index_requests.extend([REQUESTER_KEY, REQUESTER_KEY])
        timer = SelfLookupTimer(walk=SelfLookup(done=True))
        self.assertTrue(_index_handoff_step(node, timer))
        self.assertFalse(_index_handoff_step(node, timer))

    def test_index_request_waits_for_walk(self):
        node = _node(peers=[_peer(REQUESTER_KEY, 9001)])
        _index_handoff_step(node, SelfLookupTimer(walk=_open_walk()))
        self.assertFalse(node.index_requested)
        self.assertTrue(node.outgoing_queue.empty())

    def test_index_request_after_walk(self):
        a, b = _peer(REQUESTER_KEY, 9001), _peer(b"\x80" * 32, 9002)
        node = _node(peers=[a, b])
        _index_handoff_step(node, SelfLookupTimer(walk=SelfLookup(done=True)))
        self.assertTrue(node.index_requested)
        self.assertEqual(sorted(addr for addr, _ in _sent(node)), [a.address, b.address])

    def test_serve_does_not_wait_for_walk(self):
        node = _node(peers=[_peer(REQUESTER_KEY, 9001)])
        node.index_requests.append(REQUESTER_KEY)
        node.storage_index[_id(0xFF)] = (0,)
        _index_handoff_step(node, SelfLookupTimer(walk=_open_walk()))
        self.assertFalse(node.index_requested)
        sent = _sent(node)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ("127.0.0.1", 9001))


def _finished_walk():
    """A walk with one round sent whose deadline has passed."""
    return SelfLookup(rounds=1, distance=1, deadline=time.monotonic() - 1.0)


class TestBranchRequest(unittest.TestCase):
    def _node_with_route(self, keys, bucket_size=16):
        peers = [_peer(key, 9100 + i) for i, key in enumerate(keys)]
        node = _node(peers=peers)
        node.peer_route = Route(SELF_KEY, bucket_size=bucket_size)
        for peer in peers:
            node.peer_route.add_peer(peer.public_key_bytes, peer)
        node.index_requested = True
        return node, peers

    def _step(self, node, timer):
        with patch("astreum.communication.self_lookup.refresh_one_bucket", return_value=0):
            return _index_handoff_step(node, timer)

    def test_branch_request_after_later_walk(self):
        keys = [b"\x80" * 32, b"\x40" * 32, b"\x20" * 32, b"\x10" * 32]
        node, peers = self._node_with_route(keys, bucket_size=2)
        self._step(node, SelfLookupTimer(walk=_finished_walk(), joined=True))
        sent = _sent(node)
        self.assertEqual(
            sorted(addr for addr, _ in sent), sorted([peers[2].address, peers[3].address])
        )
        self.assertTrue(
            all(req.code == StorageRequestCode.STORAGE_INDEX_REQUEST for _, req in sent)
        )

    def test_no_branch_request_without_new_walk(self):
        node, _ = self._node_with_route([b"\x80" * 32])
        self._step(node, SelfLookupTimer(walk=SelfLookup(done=True), joined=True))
        self.assertTrue(node.outgoing_queue.empty())

    def test_branch_request_waits_for_latest_block(self):
        node, _ = self._node_with_route([b"\x80" * 32])
        node.latest_block = None
        self.assertFalse(_send_branch_index_requests(node))
        self.assertTrue(node.outgoing_queue.empty())

    def test_branch_request_skips_peer_without_address(self):
        node, peers = self._node_with_route([b"\x80" * 32, b"\x40" * 32])
        peers[0].address = None
        self.assertTrue(_send_branch_index_requests(node))
        self.assertEqual([addr for addr, _ in _sent(node)], [peers[1].address])

    def test_first_request_still_goes_to_every_peer(self):
        node, peers = self._node_with_route([b"\x80" * 32, b"\x40" * 32])
        node.index_requested = False
        self._step(node, SelfLookupTimer(walk=_finished_walk()))
        self.assertTrue(node.index_requested)
        self.assertEqual(
            sorted(addr for addr, _ in _sent(node)), sorted(p.address for p in peers)
        )


if __name__ == "__main__":
    unittest.main()
