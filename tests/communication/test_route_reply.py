"""Tests for the keyed ROUTE_RESPONSE: codec, reply and response handling."""
import queue
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.handlers.route_request import (
    handle_route_request,
    route_reply_entries,
)
from astreum.communication.handlers.route_response import handle_route_response
from astreum.communication.message_pow import MESSAGE_FRAMING_BYTES
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.models.route_entry import (
    decode_route_entries,
    encode_route_entry,
    max_route_entries,
    route_entry_size,
)
from astreum.communication.util import xor_distance

OWN_KEY = b"\x00" * 32
SENDER_KEY = b"\x80" * 32


def _logger():
    return SimpleNamespace(
        debug=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )


def _key(first: int, second: int = 0) -> bytes:
    return bytes([first, second]) + b"\x00" * 30


def _peer(key: bytes, host: str = "10.0.0.1", port: int = 5000, address=True) -> Peer:
    return Peer(
        node_secret_key=X25519PrivateKey.generate(),
        peer_public_key=X25519PrivateKey.generate().public_key(),
        storage_public_key_bytes=key,
        address=(host, port) if address else None,
    )


def _node(use_ipv6=False, bucket_size=16):
    node = SimpleNamespace(
        use_ipv6=use_ipv6,
        storage_public_key_bytes=OWN_KEY,
        storage_secret_key=Ed25519PrivateKey.generate(),
        relay_public_key_bytes=b"\x55" * 32,
        peers={},
        peers_lock=threading.RLock(),
        peer_route=Route(OWN_KEY, bucket_size=bucket_size),
        validation_route=Route(OWN_KEY, bucket_size=bucket_size),
        outgoing_queue=queue.Queue(),
        logger=_logger(),
    )
    return node


def _add(node, peer, route=None):
    node.peers[peer.public_key_bytes] = peer
    (route or node.peer_route).add_peer(peer.public_key_bytes, peer)
    return peer


def _request(node, requester, route_id=0):
    message = Message(
        topic=MessageTopic.ROUTE_REQUEST,
        content=bytes([route_id]),
        sender_public_key_bytes=requester.public_key_bytes,
    )
    return handle_route_request(node, requester, message)


def _reply(node, requester):
    payload, _address = node.outgoing_queue.get_nowait()
    message = Message.from_bytes(payload[8:])
    message.decrypt(requester.shared_key_bytes)
    assert message.topic == MessageTopic.ROUTE_RESPONSE
    return message.content


def _entry_keys(content, use_ipv6=False):
    return [key for key, _relay, _addr in decode_route_entries(content[1:], use_ipv6)]


class TestEntryCodec(unittest.TestCase):
    def test_entry_round_trip_ipv4(self):
        peer = _peer(_key(1), "10.1.2.3", 6000)
        entry = encode_route_entry(peer, False)
        self.assertEqual(len(entry), 70)
        self.assertEqual(
            decode_route_entries(entry, False),
            [(_key(1), peer.relay_public_key_bytes, ("10.1.2.3", 6000))],
        )

    def test_entry_round_trip_ipv6(self):
        peer = _peer(_key(1), "2001:db8::1", 6000)
        entry = encode_route_entry(peer, True)
        self.assertEqual(len(entry), 82)
        self.assertEqual(
            decode_route_entries(entry, True),
            [(_key(1), peer.relay_public_key_bytes, ("2001:db8::1", 6000))],
        )

    def test_max_entries(self):
        self.assertEqual(max_route_entries(False, 16), 16)
        self.assertEqual(max_route_entries(True, 16), 14)
        self.assertEqual(max_route_entries(False, 4), 4)

    def test_entry_of_other_family_is_not_encoded(self):
        self.assertIsNone(encode_route_entry(_peer(_key(1), "10.0.0.1"), True))
        self.assertIsNone(encode_route_entry(_peer(_key(1), "2001:db8::1"), False))
        self.assertIsNone(encode_route_entry(_peer(_key(1), address=False), False))

    def test_bad_length_raises(self):
        with self.assertRaises(ValueError):
            decode_route_entries(b"\x00" * 71, False)


class TestReply(unittest.TestCase):
    def test_reply_nearest_first(self):
        node = _node()
        for k in (_key(0x40), _key(0x10), _key(0x20)):
            _add(node, _peer(k))
        keys = [
            decode_route_entries(e, False)[0][0]
            for e in route_reply_entries(node, node.peer_route, SENDER_KEY, SENDER_KEY)
        ]
        # XOR with 0x80...: 0x10 -> 0x90, 0x20 -> 0xA0, 0x40 -> 0xC0.
        self.assertEqual(keys, [_key(0x10), _key(0x20), _key(0x40)])
        self.assertEqual(keys, sorted(keys, key=lambda k: xor_distance(SENDER_KEY, k)))

    def test_reply_excludes_sender(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        _add(node, _peer(_key(0x10)))
        self.assertTrue(_request(node, requester)[0])
        keys = _entry_keys(_reply(node, requester))
        self.assertEqual(keys, [_key(0x10)])

    def test_reply_skips_peer_without_address(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        _add(node, _peer(_key(0x10), address=False))
        _request(node, requester)
        self.assertEqual(_entry_keys(_reply(node, requester)), [])

    def test_reply_limit(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        for i in range(20):
            _add(node, _peer(_key(0x10 + i, 1)))
        _request(node, requester)
        content = _reply(node, requester)
        self.assertEqual(len(_entry_keys(content)), 16)
        self.assertLessEqual(len(content) + MESSAGE_FRAMING_BYTES, 1232)

    def test_reply_limit_ipv6(self):
        node = _node(use_ipv6=True)
        requester = _add(node, _peer(SENDER_KEY, "2001:db8::2"))
        for i in range(20):
            _add(node, _peer(_key(0x10 + i, 1), "2001:db8::1"))
        _request(node, requester)
        content = _reply(node, requester)
        self.assertEqual(len(_entry_keys(content, True)), 14)
        self.assertLessEqual(len(content) + MESSAGE_FRAMING_BYTES, 1232)

    def test_reply_has_route_id(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        _request(node, requester, route_id=0)
        self.assertEqual(_reply(node, requester)[0], 0)
        _request(node, requester, route_id=1)
        self.assertEqual(_reply(node, requester)[0], 1)

    def test_reply_route_1(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        validator = _peer(_key(0x30))
        node.peers[validator.public_key_bytes] = validator
        node.validation_route.add_peer(validator.public_key_bytes)
        _add(node, _peer(_key(0x10)))
        _request(node, requester, route_id=1)
        self.assertEqual(_entry_keys(_reply(node, requester)), [_key(0x30)])

    def test_unknown_route_id_is_refused(self):
        node = _node()
        requester = _add(node, _peer(SENDER_KEY))
        self.assertFalse(_request(node, requester, route_id=7)[0])
        self.assertTrue(node.outgoing_queue.empty())


class TestResponse(unittest.TestCase):
    def _respond(self, node, peers, route_id=0):
        content = bytes([route_id]) + b"".join(encode_route_entry(p, False) for p in peers)
        handle_route_response(node, _peer(SENDER_KEY), SimpleNamespace(content=content))
        out = []
        while not node.outgoing_queue.empty():
            payload, address = node.outgoing_queue.get_nowait()
            message = Message.from_bytes(payload[8:])
            assert message.handshake
            out.append(address)
        return out

    def test_new_entry_gets_handshake(self):
        node = _node()
        self.assertEqual(
            self._respond(node, [_peer(_key(0x10), "10.0.0.7", 7000)]),
            [("10.0.0.7", 7000)],
        )

    def test_response_skips_own_key(self):
        node = _node()
        self.assertEqual(self._respond(node, [_peer(OWN_KEY)]), [])

    def test_response_skips_known_peer(self):
        node = _node()
        known = _add(node, _peer(_key(0x10)))
        self.assertEqual(self._respond(node, [known]), [])

    def test_response_skips_full_bucket(self):
        node = _node(bucket_size=1)
        _add(node, _peer(_key(0x40)))
        self.assertEqual(self._respond(node, [_peer(_key(0x50))]), [])
        self.assertEqual(len(self._respond(node, [_peer(_key(0x10))])), 1)

    def test_response_route_1_ignores_bucket_space(self):
        node = _node(bucket_size=1)
        _add(node, _peer(_key(0x40)))
        self.assertEqual(len(self._respond(node, [_peer(_key(0x50))], route_id=1)), 1)

    def test_response_bad_length_dropped(self):
        node = _node()
        handle_route_response(node, _peer(SENDER_KEY), SimpleNamespace(content=b"\x00" + b"\x01" * 71))
        self.assertTrue(node.outgoing_queue.empty())

    def test_response_bad_route_id_dropped(self):
        node = _node()
        self.assertEqual(self._respond(node, [_peer(_key(0x10))], route_id=5), [])


if __name__ == "__main__":
    unittest.main()
