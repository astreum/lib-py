import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.storage_request import handle as put_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.workers.advertisements import (
    _index_handoff_step,
    _send_index_requests,
    _serve_one_index_request,
)
from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES

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
        config={
            "storage_public_key_bytes": SELF_KEY,
            "storage_put_batch_max_bytes": DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES,
        },
        storage_public_key_bytes=SELF_KEY,
        logger=_silent_logger(),
        outgoing_queue=queue.Queue(),
        storage_index={},
        storage_providers=[PROVIDER_A, PROVIDER_B],
        peers={peer.public_key_bytes: peer for peer in peers},
        peers_lock=threading.RLock(),
        index_requests=[],
        index_requests_lock=threading.Lock(),
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
        node.storage_index[_id(0xFF)] = 0
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
            self.node.storage_index[expr_id] = 0
        self.assertTrue(_serve_one_index_request(self.node))
        sent = _sent(self.node)
        ids = [e[0] for _, req in sent for e in req.entries]
        self.assertEqual(sorted(ids), sorted(near))

    def test_entries_use_single_resolution(self):
        self.node.storage_index[_id(0xFF)] = 0
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual(req.code, StorageRequestCode.STORAGE_PUT)
        self.assertEqual(req.entries, [(_id(0xFF), RESOLUTION_SINGLE)])

    def test_keeps_provider(self):
        self.node.storage_index[_id(0xFF, 1)] = 1
        _serve_one_index_request(self.node)
        (_, req), = _sent(self.node)
        self.assertEqual(req.data, PROVIDER_B)

    def test_groups_by_provider(self):
        self.node.storage_index[_id(0xFF, 1)] = 0
        self.node.storage_index[_id(0xFF, 2)] = 1
        self.node.storage_index[_id(0xFF, 3)] = 0
        _serve_one_index_request(self.node)
        sent = {req.data: [e[0] for e in req.entries] for _, req in _sent(self.node)}
        self.assertEqual(sorted(sent), sorted([PROVIDER_A, PROVIDER_B]))
        self.assertEqual(sorted(sent[PROVIDER_A]), [_id(0xFF, 1), _id(0xFF, 3)])
        self.assertEqual(sent[PROVIDER_B], [_id(0xFF, 2)])

    def test_keeps_own_entries(self):
        self.node.storage_index[_id(0xFF, 1)] = 0
        self.node.storage_index[_id(0x00, 1)] = 1
        before = dict(self.node.storage_index)
        _serve_one_index_request(self.node)
        self.assertEqual(self.node.storage_index, before)

    def test_unknown_peer_skipped(self):
        self.node.peers.clear()
        self.node.storage_index[_id(0xFF)] = 0
        self.assertTrue(_serve_one_index_request(self.node))
        self.assertTrue(self.node.outgoing_queue.empty())
        self.assertEqual(self.node.index_requests, [])

    def test_unknown_provider_skipped(self):
        self.node.storage_index[_id(0xFF)] = 7
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
        responder.storage_index[_id(0xFF)] = 0
        _serve_one_index_request(responder)
        (_, req), = _sent(responder)

        requester = _node()
        requester.config["storage_public_key_bytes"] = REQUESTER_KEY
        requester.peer_route = SimpleNamespace(closest_peer_for_hash=lambda h: None)
        sender = _peer(SELF_KEY, 9100)
        message = SimpleNamespace(content=req.to_bytes())
        with patch(
            "astreum.storage.admission.is_expr_in_latest_block", return_value=False
        ), patch(
            "astreum.storage.admission.get_latest_storage_account", return_value=None
        ):
            put_handle.handle_storage_request(requester, sender, message)
        self.assertEqual(requester.storage_index, {})


class TestStep(unittest.TestCase):
    def test_step_reports_remaining_requests(self):
        node = _node(peers=[_peer(REQUESTER_KEY, 9001)], synced=False)
        node.index_requests.extend([REQUESTER_KEY, REQUESTER_KEY])
        self.assertTrue(_index_handoff_step(node))
        self.assertFalse(_index_handoff_step(node))


if __name__ == "__main__":
    unittest.main()
