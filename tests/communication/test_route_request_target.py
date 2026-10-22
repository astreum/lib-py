"""Tests for the targeted ROUTE_REQUEST and the far-bucket refresh."""
import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "src")

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication import self_lookup
from astreum.communication.handlers.route_request import handle_route_request
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.models.peer_info import decode_peer_infos
from astreum.communication.self_lookup import (
    SelfLookupTimer,
    refresh_one_bucket,
    self_lookup_timer_step,
)
from astreum.communication.util import xor_distance

OWN_KEY = b"\x00" * 32
SENDER_KEY = b"\x80" * 32


def _key(first: int, second: int = 0) -> bytes:
    return bytes([first, second]) + b"\x00" * 30


def _peer(key: bytes, port: int = 5000, address=True) -> Peer:
    return Peer(
        node_secret_key=X25519PrivateKey.generate(),
        peer_public_key=X25519PrivateKey.generate().public_key(),
        storage_public_key_bytes=key,
        address=("10.0.0.1", port) if address else None,
    )


def _node(bucket_size=16, rounds=8, interval=900):
    return SimpleNamespace(
        use_ipv6=False,
        storage_public_key_bytes=OWN_KEY,
        config={
            "self_lookup_rounds": rounds,
            "self_lookup_interval": interval,
            "peer_timeout_interval": 10,
        },
        peers={},
        peers_lock=threading.RLock(),
        peer_route=Route(OWN_KEY, bucket_size=bucket_size),
        validation_route=None,
        outgoing_queue=queue.Queue(),
        logger=SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
        ),
    )


def _add(node, peer):
    node.peers[peer.public_key_bytes] = peer
    node.peer_route.add_peer(peer.public_key_bytes, peer)
    return peer


def _request(node, requester, content):
    message = Message(
        topic=MessageTopic.ROUTE_REQUEST,
        content=content,
        sender_public_key_bytes=requester.public_key_bytes,
    )
    return handle_route_request(node, requester, message)


def _reply_keys(node, requester):
    payload, _address = node.outgoing_queue.get_nowait()
    message = Message.from_bytes(payload[8:])
    message.decrypt(requester.shared_key_bytes)
    return [info.storage_key for info in decode_peer_infos(message.content[1:], False)]


def _sent(node):
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        out.append((Message.from_bytes(payload[8:]), address))
    return out


def _decrypted(message, peer):
    message.decrypt(peer.shared_key_bytes)
    return message


class TestTargetedRequest(unittest.TestCase):
    def setUp(self):
        self.node = _node()
        self.requester = _add(self.node, _peer(SENDER_KEY))
        for first in (0x40, 0x10, 0x20):
            _add(self.node, _peer(_key(first)))

    def test_one_byte_ranks_by_sender(self):
        self.assertTrue(_request(self.node, self.requester, b"\x00")[0])
        keys = _reply_keys(self.node, self.requester)
        self.assertEqual(keys, sorted(keys, key=lambda k: xor_distance(SENDER_KEY, k)))
        self.assertEqual(keys, [_key(0x10), _key(0x20), _key(0x40)])

    def test_target_ranks_by_target(self):
        target = _key(0x41)
        self.assertTrue(_request(self.node, self.requester, b"\x00" + target)[0])
        keys = _reply_keys(self.node, self.requester)
        self.assertEqual(keys, sorted(keys, key=lambda k: xor_distance(target, k)))
        self.assertEqual(keys[0], _key(0x40))

    def test_target_reply_excludes_sender(self):
        target = SENDER_KEY
        self.assertTrue(_request(self.node, self.requester, b"\x00" + target)[0])
        self.assertNotIn(SENDER_KEY, _reply_keys(self.node, self.requester))

    def test_bad_length_rejected(self):
        for content in (b"\x00" + b"\x01" * 5, b"\x00" + b"\x01" * 32 + b"\x02"):
            handled, reason = _request(self.node, self.requester, content)
            self.assertFalse(handled)
            self.assertIsNotNone(reason)
            self.assertTrue(self.node.outgoing_queue.empty())


class TestRefreshOneBucket(unittest.TestCase):
    def _spread(self, node, firsts=(0x80, 0x40, 0x20, 0x10)):
        for index, first in enumerate(firsts):
            _add(node, _peer(_key(first), 5000 + index))

    def _requests(self, node):
        """Return (peer, target, address) for each queued request."""
        out = []
        for message, address in _sent(node):
            for peer in node.peer_route.peers.values():
                try:
                    decoded = _decrypted(Message.from_bytes(message.to_bytes()), peer)
                except Exception:
                    continue
                self.assertEqual(decoded.topic, MessageTopic.ROUTE_REQUEST)
                self.assertEqual(len(decoded.content), 33)
                self.assertEqual(decoded.content[0], 0)
                out.append((peer, decoded.content[1:], address))
                break
            else:
                self.fail("request not readable by any peer")
        return out

    def test_refresh_skips_full_buckets(self):
        node = _node(bucket_size=1)
        self._spread(node, (0x80, 0x40, 0x10))
        self.assertEqual(refresh_one_bucket(node, 0), 3)
        (_peer_, target, _address), = self._requests(node)
        self.assertEqual(Route._matching_leading_bits(OWN_KEY, target), 2)

    def test_refresh_skips_buckets_past_nearest(self):
        node = _node()
        self._spread(node, (0x80, 0x40))
        for _ in range(20):
            self.assertEqual(refresh_one_bucket(node, 0), 1)
            (_peer_, target, _address), = self._requests(node)
            self.assertEqual(Route._matching_leading_bits(OWN_KEY, target), 0)
        self.assertEqual(refresh_one_bucket(node, 1), 1)

    def test_refresh_target_in_bucket(self):
        node = _node()
        self._spread(node)
        for _ in range(20):
            self.assertEqual(refresh_one_bucket(node, 2), 3)
            (_peer_, target, _address), = self._requests(node)
            self.assertEqual(Route._matching_leading_bits(OWN_KEY, target), 2)

    def test_refresh_sends_target(self):
        node = _node()
        self._spread(node)
        refresh_one_bucket(node, 1)
        (peer, target, address), = self._requests(node)
        self.assertEqual(len(target), 32)
        self.assertIs(node.peer_route.closest_peer_for_hash(target), peer)
        self.assertEqual(address, peer.address)

    def test_refresh_cursor_wraps(self):
        node = _node()
        self._spread(node)
        self.assertEqual(refresh_one_bucket(node, 3), 1)
        self.assertEqual(refresh_one_bucket(node, 1), 2)
        self.assertEqual(refresh_one_bucket(node, 0), 1)

    def test_refresh_without_peers_keeps_cursor(self):
        node = _node()
        self.assertEqual(refresh_one_bucket(node, 5), 5)
        self.assertTrue(node.outgoing_queue.empty())

    def test_refresh_with_all_full_keeps_cursor(self):
        node = _node(bucket_size=1)
        self._spread(node, (0x80, 0x40, 0x20, 0x10))
        self.assertEqual(refresh_one_bucket(node, 4), 4)

    def test_refresh_peer_without_address_sends_nothing(self):
        node = _node()
        _add(node, _peer(_key(0x80), address=False))
        _add(node, _peer(_key(0x10), address=False))
        self.assertEqual(refresh_one_bucket(node, 0), 1)
        self.assertTrue(node.outgoing_queue.empty())

    def test_refresh_runs_once_per_walk(self):
        node = _node()
        _add(node, _peer(_key(0x40)))
        timer = SelfLookupTimer()
        with mock.patch.object(self_lookup, "refresh_one_bucket", return_value=7) as refresh:
            self_lookup_timer_step(node, timer, 100.0)
            refresh.assert_not_called()
            self_lookup_timer_step(node, timer, 105.0)
            refresh.assert_not_called()
            self_lookup_timer_step(node, timer, 110.0)
            refresh.assert_called_once()
            self.assertEqual(timer.cursor, 7)
            self_lookup_timer_step(node, timer, 120.0)
            refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
