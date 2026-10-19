import logging
import queue
import threading
import time as time_module
import unittest
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from astreum.communication.models.handshake import build_handshake
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.processors import incoming as incoming_processor
from astreum.utils.config import config_setup


def _make_keypair():
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()
    sender_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, public_key, sender_bytes


class TestMessageTimestamp(unittest.TestCase):
    def setUp(self):
        self.node_private_key, self.peer_public_key, self.peer_sender_bytes = _make_keypair()
        self.sender_private_key, self.sender_public_key, self.sender_bytes = _make_keypair()

    def _encrypt_to(self, msg):
        shared = self.node_private_key.exchange(self.sender_public_key)
        msg.encrypt(shared)
        return msg

    def test_default_timestamp_is_now(self):
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"hello",
        )
        self.assertAlmostEqual(msg.timestamp, time_module.time(), delta=2)

    def test_explicit_timestamp_preserved(self):
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"hello",
            timestamp=1234567890,
        )
        self.assertEqual(msg.timestamp, 1234567890)

    def test_timestamp_roundtrip_through_encrypt_decrypt(self):
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"hello",
            timestamp=1234567890,
        )
        self._encrypt_to(msg)
        wire = msg.to_bytes()

        decoded = Message.from_bytes(wire)
        shared = self.sender_private_key.exchange(self.peer_public_key)
        decoded.decrypt(shared)

        self.assertEqual(decoded.timestamp, 1234567890)
        self.assertEqual(decoded.topic, MessageTopic.PING)
        self.assertEqual(decoded.content, b"hello")

    def test_timestamp_inside_aead_region(self):
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"hello",
            timestamp=1234567890,
        )
        self._encrypt_to(msg)
        wire = msg.to_bytes()

        # Replay with the sender's captured plaintext timestamp is impossible:
        # the timestamp only exists inside the AEAD-encrypted region, so a
        # replayer cannot rewrite it without breaking authentication.
        plaintext_topic_and_content = bytes([MessageTopic.PING.value]) + b"hello"
        self.assertNotIn(plaintext_topic_and_content, wire)

    def test_decrypt_rejects_missing_timestamp(self):
        # A payload produced by a pre-timestamp peer (topic byte + content only)
        # must fail authentication rather than yield a bogus timestamp.
        shared = self.node_private_key.exchange(self.sender_public_key)
        from astreum.crypto import chacha20poly1305
        import os

        nonce = os.urandom(12)
        ciphertext = chacha20poly1305.encrypt(
            shared, nonce, bytes([MessageTopic.PING.value]) + b"hello"
        )
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            encrypted=nonce + ciphertext,
        )
        with self.assertRaises(ValueError):
            msg.decrypt(shared)

    def test_forwarded_message_gets_fresh_timestamp(self):
        original = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"payload",
            timestamp=1234567890,
        )
        forwarded = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=original.topic,
            content=original.content,
        )
        self.assertAlmostEqual(forwarded.timestamp, time_module.time(), delta=2)
        self.assertNotEqual(forwarded.timestamp, original.timestamp)


class TestMessageTimestampWindowConfig(unittest.TestCase):
    def test_default_window_is_60(self):
        config = config_setup({})
        self.assertEqual(config["message_timestamp_window"], 60)

    def test_zero_disables_check(self):
        config = config_setup({"message_timestamp_window": 0})
        self.assertEqual(config["message_timestamp_window"], 0)

    def test_custom_window(self):
        config = config_setup({"message_timestamp_window": 120})
        self.assertEqual(config["message_timestamp_window"], 120)

    def test_negative_window_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"message_timestamp_window": -1})

    def test_non_integer_window_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"message_timestamp_window": "soon"})


class _FakeNode:
    def __init__(self, config, peer):
        self.config = config
        self.logger = logging.getLogger("test_message_timestamps")
        self.logger.setLevel(logging.CRITICAL)
        self.communication_stop_event = threading.Event()
        self.incoming_queue = queue.Queue()
        self.incoming_queue_size = 0
        self.incoming_queue_size_lock = threading.Lock()
        self.peers = {peer.public_key_bytes: peer}
        self.peers_lock = threading.Lock()


class TestReceiverWindowCheck(unittest.TestCase):
    def setUp(self):
        self.node_private_key, self.peer_relay_public_key, _ = _make_keypair()
        self.sender_private_key, self.sender_public_key, self.sender_bytes = _make_keypair()
        self.shared = self.node_private_key.exchange(self.sender_public_key)

        self.peer = Peer(
            node_secret_key=self.node_private_key,
            peer_public_key=self.sender_public_key,
            storage_public_key_bytes=self.sender_bytes,
            address=("127.0.0.1", 52780),
        )

    def _build_message(self, timestamp):
        msg = Message(
            handshake=False,
            sender=self.sender_public_key,
            topic=MessageTopic.PING,
            content=b"ping",
            timestamp=timestamp,
        )
        msg.encrypt(self.shared)
        return msg

    def _run_processor(self, node):
        thread = threading.Thread(
            target=incoming_processor.process_incoming_messages,
            args=(node,),
            daemon=True,
        )
        thread.start()

        # Wait for the queue to drain, then a beat for the item to finish
        # processing, then signal stop. The processor exits on its next check.
        deadline = time_module.time() + 5
        while time_module.time() < deadline and not node.incoming_queue.empty():
            time_module.sleep(0.01)
        time_module.sleep(0.2)
        node.communication_stop_event.set()

        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "processor did not stop")

    def test_stale_timestamp_dropped_before_dispatch(self):
        node = _FakeNode({"message_timestamp_window": 60}, self.peer)
        stale = self._build_message(int(time_module.time()) - 3600)

        with mock.patch.object(incoming_processor, "handle_ping") as handler:
            node.incoming_queue.put((stale.to_bytes(), ("127.0.0.1", 52780), 100, 100))
            self._run_processor(node)
            handler.assert_not_called()

    def test_future_timestamp_beyond_window_dropped(self):
        node = _FakeNode({"message_timestamp_window": 60}, self.peer)
        future = self._build_message(int(time_module.time()) + 3600)

        with mock.patch.object(incoming_processor, "handle_ping") as handler:
            node.incoming_queue.put((future.to_bytes(), ("127.0.0.1", 52780), 100, 100))
            self._run_processor(node)
            handler.assert_not_called()

    def test_in_window_timestamp_dispatches(self):
        node = _FakeNode({"message_timestamp_window": 60}, self.peer)
        fresh = self._build_message(int(time_module.time()))

        with mock.patch.object(incoming_processor, "handle_ping") as handler:
            node.incoming_queue.put((fresh.to_bytes(), ("127.0.0.1", 52780), 100, 100))
            self._run_processor(node)
            handler.assert_called_once()
            args = handler.call_args[0]
            self.assertIs(args[0], node)
            self.assertIs(args[1], self.peer)
            self.assertEqual(args[2], b"ping")

    def test_window_zero_disables_check(self):
        node = _FakeNode({"message_timestamp_window": 0}, self.peer)
        stale = self._build_message(int(time_module.time()) - 3600)

        with mock.patch.object(incoming_processor, "handle_ping") as handler:
            node.incoming_queue.put((stale.to_bytes(), ("127.0.0.1", 52780), 100, 100))
            self._run_processor(node)
            handler.assert_called_once()

    def test_queue_size_accounting_on_drop(self):
        node = _FakeNode({"message_timestamp_window": 60}, self.peer)
        stale = self._build_message(int(time_module.time()) - 3600)
        node.incoming_queue.put((stale.to_bytes(), ("127.0.0.1", 52780), 100, 100))
        node.incoming_queue_size = 100
        self._run_processor(node)
        self.assertEqual(node.incoming_queue_size, 0)

    def test_handshake_exempt_from_window(self):
        # The processor does not apply the encrypted-message window to a
        # handshake. handle_handshake checks the handshake timestamp itself.
        node = _FakeNode({"message_timestamp_window": 60}, self.peer)
        sender = SimpleNamespace(
            storage_secret_key=Ed25519PrivateKey.generate(),
            relay_public_key_bytes=self.peer_relay_public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ),
        )
        sender.storage_public_key_bytes = sender.storage_secret_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        handshake = build_handshake(
            sender, reply_requested=True, now=int(time_module.time()) - 3600
        )

        with mock.patch.object(incoming_processor, "handle_handshake", return_value=True):
            node.incoming_queue.put((handshake.to_bytes(), ("127.0.0.1", 52780), 100, 100))
            with mock.patch.object(incoming_processor, "handle_ping") as handler:
                self._run_processor(node)
                handler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
