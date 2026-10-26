import logging
import queue
import sys
import threading
import time as time_module
import unittest
import unittest.mock
from types import SimpleNamespace

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.handlers.handshake import handle_handshake
from astreum.communication.models.handshake import build_handshake, parse_handshake
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.models.relay_endpoint import RelayEndpoint
from astreum.communication.processors import incoming as incoming_processor
from astreum.consensus.block.holders import (
    block_holder,
    init_block_holders,
    record_block_holder,
)
from astreum.utils.config import NodeConfig

WINDOW = 60
RAW = dict(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)


def _node(default_seed_ips=()):
    storage_secret = Ed25519PrivateKey.generate()
    storage_public = storage_secret.public_key().public_bytes(**RAW)
    relay_secret = X25519PrivateKey.generate()
    return SimpleNamespace(
        storage_secret_key=storage_secret,
        storage_public_key_bytes=storage_public,
        relay_secret_key=relay_secret,
        relay_public_key_bytes=relay_secret.public_key().public_bytes(**RAW),
        config=NodeConfig(message_timestamp_window=WINDOW),
        logger=logging.getLogger("test_peer_update_rules"),
        default_seed_ips=set(default_seed_ips),
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


def _restart(node):
    """Give the node a new relay key, as after a restart."""
    node.relay_secret_key = X25519PrivateKey.generate()
    node.relay_public_key_bytes = node.relay_secret_key.public_key().public_bytes(
        **RAW
    )
    node.peers = {}
    node.peer_route = Route(node.storage_public_key_bytes)


def _drain(node):
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        out.append((Message.from_bytes(payload[8:]), address))
    return out


def _handshake_from(remote, reply_requested, offset=0):
    return build_handshake(
        remote, reply_requested, now=int(time_module.time()) + offset
    )


def _add_known(node, remote, address, handshake_timestamp=None):
    """Make node know remote through a real handshake."""
    stamp = int(time_module.time()) - 10 if handshake_timestamp is None else handshake_timestamp
    message = build_handshake(remote, reply_requested=False, now=stamp)
    handle_handshake(node, address, message)
    _drain(node)
    return node.peers[remote.storage_public_key_bytes]


def _decrypts(message, node, remote):
    peer = node.peers[remote.storage_public_key_bytes]
    message.decrypt(peer.shared_key_bytes)
    return message.topic


class TestPeerRelayKey(unittest.TestCase):
    def test_set_relay_key_matches_new_peer(self):
        node_secret = X25519PrivateKey.generate()
        first = X25519PrivateKey.generate().public_key()
        second = X25519PrivateKey.generate().public_key()
        peer = Peer(
            node_secret_key=node_secret,
            peer_public_key=first,
            storage_public_key_bytes=b"\x01" * 32,
        )
        peer.set_relay_key(node_secret, second.public_bytes(**RAW), 123)
        fresh = Peer(
            node_secret_key=node_secret,
            peer_public_key=second,
            storage_public_key_bytes=b"\x01" * 32,
        )
        self.assertEqual(peer.shared_key_bytes, fresh.shared_key_bytes)
        self.assertEqual(peer.relay_public_key_bytes, fresh.relay_public_key_bytes)
        self.assertEqual(peer.handshake_timestamp, 123)

    def test_set_relay_key_bad_key_raises(self):
        node_secret = X25519PrivateKey.generate()
        peer = Peer(
            node_secret_key=node_secret,
            peer_public_key=X25519PrivateKey.generate().public_key(),
            storage_public_key_bytes=b"\x01" * 32,
            handshake_timestamp=5,
        )
        before = (peer.shared_key_bytes, peer.relay_public_key_bytes)
        with self.assertRaises(ValueError):
            peer.set_relay_key(node_secret, b"\x00" * 31, 99)
        self.assertEqual(before, (peer.shared_key_bytes, peer.relay_public_key_bytes))
        self.assertEqual(peer.handshake_timestamp, 5)


class TestKnownPeerHandshake(unittest.TestCase):
    def setUp(self):
        self.node = _node(default_seed_ips={"10.0.0.9"})
        self.remote = _node()
        self.addr = ("10.0.0.5", 5000)
        self.peer = _add_known(self.node, self.remote, self.addr)

    def test_handshake_does_not_change_address(self):
        handle_handshake(
            self.node, ("10.9.9.9", 7777), _handshake_from(self.remote, False)
        )
        self.assertEqual(self.peer.address, self.addr)

    def test_handshake_does_not_change_default_seed(self):
        self.assertFalse(self.peer.is_default_seed)
        handle_handshake(
            self.node, ("10.0.0.9", 7777), _handshake_from(self.remote, False)
        )
        self.assertFalse(self.peer.is_default_seed)

    def test_newer_relay_key_replaces_session(self):
        old_shared = self.peer.shared_key_bytes
        old_route_peer = self.node.peer_route.closest_peer_for_hash(
            self.remote.storage_public_key_bytes
        )
        _restart(self.remote)
        message = _handshake_from(self.remote, False)
        new_stamp = parse_handshake(message).timestamp
        handle_handshake(self.node, self.addr, message)
        self.assertIs(self.node.peers[self.remote.storage_public_key_bytes], self.peer)
        self.assertEqual(
            self.peer.relay_public_key_bytes, self.remote.relay_public_key_bytes
        )
        self.assertNotEqual(self.peer.shared_key_bytes, old_shared)
        self.assertEqual(self.peer.handshake_timestamp, new_stamp)
        self.assertIs(old_route_peer, self.peer)

    def _holder(self, relay_key):
        init_block_holders(self.node)
        record_block_holder(self.node, b"\x31" * 32, RelayEndpoint(relay_key, self.addr))

    def test_key_change_updates_block_holders(self):
        self._holder(self.remote.relay_public_key_bytes)
        _restart(self.remote)
        handle_handshake(self.node, self.addr, _handshake_from(self.remote, False))
        self.assertEqual(
            block_holder(self.node, b"\x31" * 32).relay_key,
            self.remote.relay_public_key_bytes,
        )
        self.assertEqual(block_holder(self.node, b"\x31" * 32).address, self.addr)

    def test_older_handshake_leaves_holders(self):
        old_key = self.remote.relay_public_key_bytes
        self._holder(old_key)
        stored = self.peer.handshake_timestamp
        _restart(self.remote)
        handle_handshake(self.node, self.addr, build_handshake(self.remote, False, now=stored - 5))
        self.assertEqual(block_holder(self.node, b"\x31" * 32).relay_key, old_key)

    def test_same_key_leaves_holders(self):
        old_key = self.remote.relay_public_key_bytes
        self._holder(old_key)
        handle_handshake(self.node, self.addr, _handshake_from(self.remote, False))
        self.assertEqual(block_holder(self.node, b"\x31" * 32).relay_key, old_key)

    def test_rejected_key_leaves_holders(self):
        old_key = self.remote.relay_public_key_bytes
        self._holder(old_key)
        _restart(self.remote)
        message = _handshake_from(self.remote, False)
        with unittest.mock.patch.object(Peer, "set_relay_key", side_effect=ValueError("bad")):
            handle_handshake(self.node, self.addr, message)
        self.assertEqual(block_holder(self.node, b"\x31" * 32).relay_key, old_key)

    def test_older_relay_key_dropped(self):
        stored = self.peer.handshake_timestamp
        old_key = self.peer.relay_public_key_bytes
        old_shared = self.peer.shared_key_bytes
        _restart(self.remote)
        for stamp in (stored, stored - 5):
            message = build_handshake(self.remote, True, now=stamp)
            self.assertTrue(handle_handshake(self.node, self.addr, message))
            self.assertEqual(self.peer.relay_public_key_bytes, old_key)
            self.assertEqual(self.peer.shared_key_bytes, old_shared)
            self.assertEqual(self.peer.handshake_timestamp, stored)
        self.assertTrue(self.node.outgoing_queue.empty())

    def test_same_relay_key_moves_timestamp_forward(self):
        stored = self.peer.handshake_timestamp
        handle_handshake(
            self.node, self.addr, build_handshake(self.remote, False, now=stored + 5)
        )
        self.assertEqual(self.peer.handshake_timestamp, stored + 5)
        handle_handshake(
            self.node, self.addr, build_handshake(self.remote, False, now=stored + 2)
        )
        self.assertEqual(self.peer.handshake_timestamp, stored + 5)

    def test_reply_requested_gets_handshake_and_ping(self):
        source = ("10.9.9.9", 7777)
        handle_handshake(self.node, source, _handshake_from(self.remote, True))
        sent = _drain(self.node)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(address == source for _, address in sent))
        handshakes = [m for m, _ in sent if m.handshake]
        pings = [m for m, _ in sent if not m.handshake]
        self.assertEqual(len(handshakes), 1)
        self.assertEqual(len(pings), 1)
        result = parse_handshake(handshakes[0])
        self.assertIsNotNone(result)
        self.assertFalse(result.reply_requested)
        self.assertEqual(_decrypts(pings[0], self.node, self.remote), MessageTopic.PING)

    def test_no_reply_requested_gets_ping_only(self):
        source = ("10.9.9.9", 7777)
        handle_handshake(self.node, source, _handshake_from(self.remote, False))
        sent = _drain(self.node)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][0].handshake)
        self.assertEqual(sent[0][1], source)

    def test_known_peer_handshake_returns_true(self):
        self.assertTrue(
            handle_handshake(self.node, self.addr, _handshake_from(self.remote, False))
        )


class TestNewPeerHandshake(unittest.TestCase):
    def test_new_peer_gets_handshake_timestamp(self):
        node, remote = _node(), _node()
        message = build_handshake(remote, True, now=int(time_module.time()) - 3)
        stamp = parse_handshake(message).timestamp
        handle_handshake(node, ("10.0.0.5", 5000), message)
        peer = node.peers[remote.storage_public_key_bytes]
        self.assertEqual(peer.handshake_timestamp, stamp)


class TestIncomingAddress(unittest.TestCase):
    def setUp(self):
        self.node = _node(default_seed_ips={"10.0.0.9"})
        self.remote = _node()
        self.addr = ("10.0.0.5", 5000)
        self.peer = _add_known(self.node, self.remote, self.addr)

    def _ping(self, shared=None, timestamp=None):
        message = Message(
            topic=MessageTopic.PING,
            content=b"ping",
            sender_public_key_bytes=self.remote.storage_public_key_bytes,
            timestamp=int(time_module.time()) if timestamp is None else timestamp,
        )
        message.encrypt(self.peer.shared_key_bytes if shared is None else shared)
        return message

    def _run(self, message, addr):
        self.node.incoming_queue.put((message.to_bytes(), addr, 100, 100))
        thread = threading.Thread(
            target=incoming_processor.process_incoming_messages,
            args=(self.node,),
            daemon=True,
        )
        thread.start()
        deadline = time_module.time() + 5
        while time_module.time() < deadline and not self.node.incoming_queue.empty():
            time_module.sleep(0.01)
        time_module.sleep(0.2)
        self.node.communication_stop_event.set()
        thread.join(timeout=2)

    def test_decrypted_message_changes_address(self):
        self._run(self._ping(), ("10.0.0.6", 6000))
        self.assertEqual(self.peer.address, ("10.0.0.6", 6000))

    def test_undecryptable_message_keeps_address(self):
        wrong = X25519PrivateKey.generate().exchange(
            X25519PrivateKey.generate().public_key()
        )
        self._run(self._ping(shared=wrong), ("10.0.0.6", 6000))
        self.assertEqual(self.peer.address, self.addr)

    def test_stale_message_keeps_address(self):
        stale = self._ping(timestamp=int(time_module.time()) - 3600)
        self._run(stale, ("10.0.0.6", 6000))
        self.assertEqual(self.peer.address, self.addr)

    def test_address_change_sets_default_seed(self):
        self._run(self._ping(), ("10.0.0.9", 6000))
        self.assertTrue(self.peer.is_default_seed)

    def test_address_change_clears_default_seed(self):
        self.peer.is_default_seed = True
        self._run(self._ping(), ("10.0.0.6", 6000))
        self.assertFalse(self.peer.is_default_seed)

    def test_handshake_does_not_reach_address_update(self):
        self._run(_handshake_from(self.remote, False), ("10.0.0.6", 6000))
        self.assertEqual(self.peer.address, self.addr)
        sent = _drain(self.node)
        self.assertEqual(len([m for m, _ in sent if not m.handshake]), 1)
        self.assertEqual(len([m for m, _ in sent if m.handshake]), 0)


class TestRestart(unittest.TestCase):
    ADDR_A = ("10.0.0.1", 1000)
    ADDR_B = ("10.0.0.2", 2000)

    def _exchange(self, a, b):
        """Pass queued packets between a and b until none are left."""
        handshakes = 0
        for _ in range(20):
            moved = False
            for sender, receiver, source, own_address in (
                (a, b, self.ADDR_A, self.ADDR_B),
                (b, a, self.ADDR_B, self.ADDR_A),
            ):
                for message, address in _drain(sender):
                    moved = True
                    if message.handshake:
                        handshakes += 1
                        handle_handshake(receiver, source, message)
                    else:
                        self._deliver(receiver, sender, message, source)
            if not moved:
                return handshakes
        self.fail("exchange did not end")

    def _deliver(self, receiver, sender, message, source):
        peer = receiver.peers.get(sender.storage_public_key_bytes)
        self.assertIsNotNone(peer, "ping from a node that is not a known peer")
        message.decrypt(peer.shared_key_bytes)

    def _ping_works(self, a, b):
        for sender, receiver in ((a, b), (b, a)):
            peer = receiver.peers[sender.storage_public_key_bytes]
            known = sender.peers[receiver.storage_public_key_bytes]
            message = Message(
                topic=MessageTopic.PING,
                content=b"ping",
                sender_public_key_bytes=sender.storage_public_key_bytes,
            )
            message.encrypt(known.shared_key_bytes)
            message.decrypt(peer.shared_key_bytes)
            self.assertEqual(message.topic, MessageTopic.PING)

    def _connect(self):
        a, b = _node(), _node()
        handle_handshake(a, self.ADDR_B, _handshake_from(b, True))
        handle_handshake(b, self.ADDR_A, _handshake_from(a, True))
        self._exchange(a, b)
        self._ping_works(a, b)
        return a, b

    def test_restart_session_recovers(self):
        a, b = self._connect()
        _restart(b)
        time_module.sleep(1.1)
        handle_handshake(a, self.ADDR_B, _handshake_from(b, True))
        handshakes = self._exchange(a, b)
        self.assertLess(handshakes, 6)
        self._ping_works(a, b)

    def test_restart_same_relay_key_recovers(self):
        a, b = self._connect()
        b.peers = {}
        b.peer_route = Route(b.storage_public_key_bytes)
        handle_handshake(a, self.ADDR_B, _handshake_from(b, True))
        handshakes = self._exchange(a, b)
        self.assertLess(handshakes, 6)
        self._ping_works(a, b)


if __name__ == "__main__":
    unittest.main()
