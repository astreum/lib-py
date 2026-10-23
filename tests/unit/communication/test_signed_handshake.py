import logging
import queue
import sys
import threading
import time as time_module
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.handlers.handshake import handle_handshake
from astreum.communication.handlers.route_response import handle_route_response
from astreum.communication.models.handshake import (
    HANDSHAKE_CONTENT_SIZE,
    build_handshake,
    Handshake,
    handshake_signed_bytes,
    parse_handshake,
    verify_handshake,
)
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.models.peer_info import PeerInfo, encode_peer_info
from astreum.communication.processors import incoming as incoming_processor
from astreum.communication.processors.peer import _queue_bootstrap_handshakes
from astreum.utils.config import NodeConfig

WINDOW = 60
RAW = dict(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)


def _node(window=WINDOW, bootstrap_peers=()):
    storage_secret = Ed25519PrivateKey.generate()
    storage_public = storage_secret.public_key().public_bytes(**RAW)
    relay_secret = X25519PrivateKey.generate()
    relay_public = relay_secret.public_key().public_bytes(**RAW)
    return SimpleNamespace(
        storage_secret_key=storage_secret,
        storage_public_key_bytes=storage_public,
        relay_secret_key=relay_secret,
        relay_public_key_bytes=relay_public,
        config=NodeConfig(message_timestamp_window=window),
        logger=logging.getLogger("test_signed_handshake"),
        default_seed_ips=set(),
        bootstrap_peers=list(bootstrap_peers),
        peers={},
        peers_lock=threading.RLock(),
        peer_route=Route(storage_public),
        outgoing_queue=queue.Queue(),
        latest_block_lock=threading.Lock(),
        latest_block_hash=None,
        latest_block=None,
        incoming_queue=queue.Queue(),
        incoming_queue_size=0,
        incoming_queue_size_limit=0,
        incoming_queue_size_lock=threading.Lock(),
        communication_stop_event=threading.Event(),
    )


def _drain(node):
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        out.append((Message.from_bytes(payload[8:]), address))
    return out


def _handshakes(node):
    return [(m, a) for m, a in _drain(node) if m.handshake]


def _verify(message, now=None, window=WINDOW):
    handshake = parse_handshake(message)
    if handshake is None:
        return None
    now = time_module.time() if now is None else now
    if not verify_handshake(message, handshake, now, window):
        return None
    return handshake


def _tamper(message, index):
    content = bytearray(message.content)
    content[index] ^= 0x01
    message.content = bytes(content)
    return message


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.node = _node()

    def test_build_layout(self):
        message = build_handshake(self.node, reply_requested=True, now=1000)
        self.assertTrue(message.handshake)
        self.assertEqual(
            message.sender_public_key_bytes, self.node.storage_public_key_bytes
        )
        content = message.content
        self.assertEqual(len(content), HANDSHAKE_CONTENT_SIZE)
        self.assertEqual(content[0], 0x01)
        self.assertEqual(content[1:33], self.node.relay_public_key_bytes)
        self.assertEqual(int.from_bytes(content[33:41], "big"), 1000)
        self.assertEqual(len(content[41:105]), 64)

    def test_build_reply_requested(self):
        yes = build_handshake(self.node, reply_requested=True)
        no = build_handshake(self.node, reply_requested=False)
        self.assertEqual(yes.content[0], 0x01)
        self.assertEqual(no.content[0], 0x00)

    def test_build_signature_valid(self):
        message = build_handshake(self.node, reply_requested=True, now=1000)
        signed = handshake_signed_bytes(
            self.node.storage_public_key_bytes,
            message.content[0:1],
            message.content[1:33],
            1000,
        )
        self.assertEqual(len(signed), 73)
        self.node.storage_secret_key.public_key().verify(
            message.content[41:105], signed
        )

    def test_build_survives_wire_roundtrip(self):
        message = build_handshake(self.node, reply_requested=True)
        decoded = Message.from_bytes(message.to_bytes())
        self.assertIsNotNone(_verify(decoded))


class TestParse(unittest.TestCase):
    def setUp(self):
        self.node = _node()

    def test_parse_returns_fields(self):
        message = build_handshake(self.node, reply_requested=True, now=1000)
        result = parse_handshake(message)
        self.assertIsInstance(result, Handshake)
        self.assertEqual(result.relay_key, self.node.relay_public_key_bytes)
        self.assertEqual(result.timestamp, 1000)
        self.assertTrue(result.reply_requested)
        self.assertEqual(result.signature, message.content[41:105])
        self.assertEqual(len(result.signature), 64)

    def test_parse_reply_requested_false(self):
        message = build_handshake(self.node, reply_requested=False)
        self.assertFalse(parse_handshake(message).reply_requested)

    def test_parse_rejects_bad_length(self):
        message = build_handshake(self.node, reply_requested=True)
        message.content = message.content[:-1]
        self.assertIsNone(parse_handshake(message))
        message.content = message.content + b"\x00\x00"
        self.assertIsNone(parse_handshake(message))

    def test_parse_rejects_invalid_reply_byte(self):
        message = build_handshake(self.node, reply_requested=True)
        message.content = b"\x02" + message.content[1:]
        self.assertIsNone(parse_handshake(message))

    def test_parse_does_not_check_signature(self):
        message = _tamper(build_handshake(self.node, reply_requested=True), 104)
        self.assertIsNotNone(parse_handshake(message))


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.node = _node()

    def _built(self, reply_requested=True, now=None):
        return build_handshake(self.node, reply_requested, now=now)

    def test_verify_accepts_valid(self):
        now = int(time_module.time())
        message = self._built(True, now=now)
        result = _verify(message, now=now)
        self.assertIsInstance(result, Handshake)
        self.assertEqual(result.relay_key, self.node.relay_public_key_bytes)
        self.assertEqual(result.timestamp, now)
        self.assertTrue(result.reply_requested)

    def test_verify_returns_false_reply_requested(self):
        now = int(time_module.time())
        result = _verify(self._built(False, now=now), now=now)
        self.assertFalse(result.reply_requested)

    def test_verify_returns_bool(self):
        now = int(time_module.time())
        message = self._built(True, now=now)
        handshake = parse_handshake(message)
        self.assertIs(verify_handshake(message, handshake, now, WINDOW), True)

    def test_verify_false_for_tampered_signature(self):
        now = int(time_module.time())
        message = _tamper(self._built(True, now=now), 104)
        handshake = parse_handshake(message)
        self.assertIs(verify_handshake(message, handshake, now, WINDOW), False)

    def test_verify_rejects_old_form(self):
        message = Message(
            handshake=True,
            sender_public_key_bytes=self.node.storage_public_key_bytes,
            content=self.node.relay_public_key_bytes,
        )
        self.assertIsNone(_verify(message))

    def test_verify_rejects_bad_length(self):
        message = self._built()
        message.content = message.content[:-1]
        self.assertIsNone(_verify(message))
        message.content = message.content + b"\x00\x00"
        self.assertIsNone(_verify(message))

    def test_verify_rejects_bad_signature(self):
        self.assertIsNone(_verify(_tamper(self._built(), 41)))
        self.assertIsNone(_verify(_tamper(self._built(), 104)))

    def test_verify_rejects_changed_relay_key(self):
        self.assertIsNone(_verify(_tamper(self._built(), 1)))

    def test_verify_rejects_changed_timestamp(self):
        self.assertIsNone(_verify(_tamper(self._built(), 40)))

    def test_verify_rejects_changed_reply_requested(self):
        message = self._built(reply_requested=False)
        message.content = b"\x01" + message.content[1:]
        self.assertIsNone(_verify(message))

    def test_verify_rejects_other_storage_id(self):
        message = self._built()
        message.sender_public_key_bytes = _node().storage_public_key_bytes
        self.assertIsNone(_verify(message))

    def test_verify_rejects_invalid_reply_requested(self):
        now = int(time_module.time())
        relay_key = self.node.relay_public_key_bytes
        signed = handshake_signed_bytes(
            self.node.storage_public_key_bytes, b"\x02", relay_key, now
        )
        signature = self.node.storage_secret_key.sign(signed)
        message = Message(
            handshake=True,
            sender_public_key_bytes=self.node.storage_public_key_bytes,
            content=b"\x02" + relay_key + now.to_bytes(8, "big") + signature,
        )
        self.assertIsNone(_verify(message, now=now))

    def test_verify_rejects_old_timestamp(self):
        now = int(time_module.time())
        message = self._built(now=now - WINDOW - 1)
        self.assertIsNone(_verify(message, now=now))

    def test_verify_rejects_future_timestamp(self):
        now = int(time_module.time())
        message = self._built(now=now + WINDOW + 1)
        self.assertIsNone(_verify(message, now=now))

    def test_verify_window_zero_skips_time(self):
        now = int(time_module.time())
        message = self._built(now=now - 86400)
        self.assertIsNotNone(_verify(message, now=now, window=0))

    def test_verify_rejects_bad_storage_id(self):
        message = self._built()
        message.sender_public_key_bytes = b"\xff" * 32
        self.assertIsNone(_verify(message))


class TestHandleHandshake(unittest.TestCase):
    def setUp(self):
        self.node = _node()
        self.remote = _node()
        self.addr = ("10.0.0.9", 6000)

    def test_valid_handshake_adds_peer(self):
        message = build_handshake(self.remote, reply_requested=True)
        self.assertTrue(handle_handshake(self.node, self.addr, message))
        peer = self.node.peers[self.remote.storage_public_key_bytes]
        self.assertEqual(
            peer.relay_public_key_bytes, self.remote.relay_public_key_bytes
        )
        self.assertEqual(peer.address, self.addr)

    def test_invalid_handshake_dropped(self):
        message = _tamper(build_handshake(self.remote, reply_requested=True), 50)
        self.assertTrue(handle_handshake(self.node, self.addr, message))
        self.assertEqual(self.node.peers, {})
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_stale_handshake_dropped(self):
        old = int(time_module.time()) - WINDOW - 5
        message = build_handshake(self.remote, reply_requested=True, now=old)
        self.assertTrue(handle_handshake(self.node, self.addr, message))
        self.assertEqual(self.node.peers, {})

    def test_invalid_handshake_does_not_change_known_peer(self):
        good = build_handshake(self.remote, reply_requested=True)
        handle_handshake(self.node, self.addr, good)
        _drain(self.node)
        bad = _tamper(build_handshake(self.remote, reply_requested=True), 50)
        handle_handshake(self.node, ("10.9.9.9", 7777), bad)
        peer = self.node.peers[self.remote.storage_public_key_bytes]
        self.assertEqual(peer.address, self.addr)
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_reply_is_signed_without_reply_requested(self):
        message = build_handshake(self.remote, reply_requested=True)
        handle_handshake(self.node, self.addr, message)
        sent = _handshakes(self.node)
        self.assertEqual(len(sent), 1)
        reply, address = sent[0]
        self.assertEqual(address, self.addr)
        result = _verify(reply)
        self.assertIsNotNone(result)
        self.assertEqual(result.relay_key, self.node.relay_public_key_bytes)
        self.assertFalse(result.reply_requested)


class TestSenders(unittest.TestCase):
    def test_bootstrap_retry_handshake_signed(self):
        node = _node(bootstrap_peers=["127.0.0.1:52780"])
        self.assertEqual(_queue_bootstrap_handshakes(node), 1)
        sent = _handshakes(node)
        self.assertEqual(len(sent), 1)
        result = _verify(sent[0][0])
        self.assertIsNotNone(result)
        self.assertTrue(result.reply_requested)
        self.assertEqual(
            sent[0][0].sender_public_key_bytes, node.storage_public_key_bytes
        )

    def test_route_response_handshake_signed(self):
        node = _node()
        remote = _node()
        entry_peer = Peer(
            node_secret_key=X25519PrivateKey.generate(),
            peer_public_key=X25519PrivateKey.generate().public_key(),
            storage_public_key_bytes=remote.storage_public_key_bytes,
            address=("10.0.0.7", 7000),
        )
        content = b"\x00" + encode_peer_info(
            PeerInfo(
                entry_peer.public_key_bytes,
                entry_peer.relay_public_key_bytes,
                entry_peer.address,
            ),
            False,
        )
        node.use_ipv6 = False
        handle_route_response(
            node, entry_peer, SimpleNamespace(content=content)
        )
        sent = _handshakes(node)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], ("10.0.0.7", 7000))
        result = _verify(sent[0][0])
        self.assertIsNotNone(result)
        self.assertTrue(result.reply_requested)

    def _run_processor(self, node):
        thread = threading.Thread(
            target=incoming_processor.process_incoming_messages,
            args=(node,),
            daemon=True,
        )
        thread.start()
        deadline = time_module.time() + 5
        while time_module.time() < deadline and not node.incoming_queue.empty():
            time_module.sleep(0.01)
        time_module.sleep(0.2)
        node.communication_stop_event.set()
        thread.join(timeout=2)

    def test_unknown_peer_handshake_signed(self):
        node = _node()
        remote = _node()
        secret = X25519PrivateKey.generate()
        shared = secret.exchange(X25519PrivateKey.generate().public_key())
        message = Message(
            topic=MessageTopic.PING,
            content=b"ping",
            sender_public_key_bytes=remote.storage_public_key_bytes,
        )
        message.encrypt(shared)
        addr = ("10.0.0.5", 5555)
        node.incoming_queue.put((message.to_bytes(), addr, 100, 100))
        self._run_processor(node)
        sent = _handshakes(node)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], addr)
        result = _verify(sent[0][0])
        self.assertIsNotNone(result)
        self.assertTrue(result.reply_requested)

    def test_decrypt_failure_handshake_signed(self):
        node = _node()
        remote = _node()
        peer = Peer(
            node_secret_key=node.relay_secret_key,
            peer_public_key=remote.relay_secret_key.public_key(),
            storage_public_key_bytes=remote.storage_public_key_bytes,
            address=("10.0.0.5", 5555),
        )
        node.peers[peer.public_key_bytes] = peer
        wrong_key = X25519PrivateKey.generate().exchange(
            X25519PrivateKey.generate().public_key()
        )
        message = Message(
            topic=MessageTopic.PING,
            content=b"ping",
            sender_public_key_bytes=remote.storage_public_key_bytes,
        )
        message.encrypt(wrong_key)
        node.incoming_queue.put((message.to_bytes(), peer.address, 100, 100))
        self._run_processor(node)
        sent = _handshakes(node)
        self.assertEqual(len(sent), 1)
        result = _verify(sent[0][0])
        self.assertIsNotNone(result)
        self.assertTrue(result.reply_requested)


if __name__ == "__main__":
    unittest.main()
