"""Tests for peer sync: message refresh, timer walk and keep-alive."""
import logging
import queue
import sys
import threading
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.handlers.handshake import handle_handshake
from astreum.communication.models.handshake import build_handshake, parse_handshake, verify_handshake
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.processors import incoming as incoming_processor
from astreum.communication.processors.peer import _manage_peer_pass
from astreum.communication.self_lookup import SelfLookup, SelfLookupTimer, self_lookup_timer_step
from astreum.utils.config import DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS, config_setup
from astreum.utils.config import NodeConfig

RAW = dict(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
OWN_KEY = b"\x00" * 32
TIMEOUT_INTERVAL = 10
INTERVAL = 900


def _key(first: int) -> bytes:
    return bytes([first]) + b"\x00" * 31


def _full_node(window=60):
    storage_secret = Ed25519PrivateKey.generate()
    storage_public = storage_secret.public_key().public_bytes(**RAW)
    relay_secret = X25519PrivateKey.generate()
    return SimpleNamespace(
        storage_secret_key=storage_secret,
        storage_public_key_bytes=storage_public,
        relay_secret_key=relay_secret,
        relay_public_key_bytes=relay_secret.public_key().public_bytes(**RAW),
        config=NodeConfig(
            message_timestamp_window=window,
            peer_timeout=900,
            peer_timeout_interval=TIMEOUT_INTERVAL,
            bootstrap_retry_interval=0,
        ),
        logger=logging.getLogger("test_peer_sync"),
        default_seed_ips=set(),
        bootstrap_peers=[],
        peers={},
        peers_lock=threading.RLock(),
        peer_route=Route(storage_public),
        outgoing_queue=queue.Queue(),
        latest_block_lock=threading.Lock(),
        latest_block_hash=None,
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


def _old(seconds=3600):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class TestMessageRefresh(unittest.TestCase):
    def setUp(self):
        self.node = _full_node()
        self.remote = _full_node()
        self.addr = ("10.0.0.5", 5000)
        handle_handshake(
            self.node,
            self.addr,
            build_handshake(self.remote, False, now=int(time_module.time()) - 10),
        )
        _drain(self.node)
        self.peer = self.node.peers[self.remote.storage_public_key_bytes]
        self.peer.timestamp = _old()

    def _request(self, timestamp=None):
        message = Message(
            topic=MessageTopic.STORAGE_REQUEST,
            content=b"\x00",
            sender_public_key_bytes=self.remote.storage_public_key_bytes,
            timestamp=int(time_module.time()) if timestamp is None else timestamp,
        )
        message.encrypt(self.peer.shared_key_bytes)
        return message

    def _run(self, message):
        self.node.incoming_queue.put((message.to_bytes(), self.addr, 100, 100))
        thread = threading.Thread(
            target=incoming_processor.process_incoming_messages,
            args=(self.node,),
            daemon=True,
        )
        with mock.patch.object(
            incoming_processor, "handle_storage_request", return_value=(True, None)
        ):
            thread.start()
            deadline = time_module.time() + 5
            while time_module.time() < deadline and not self.node.incoming_queue.empty():
                time_module.sleep(0.01)
            time_module.sleep(0.2)
            self.node.communication_stop_event.set()
            thread.join(timeout=2)

    def test_message_refreshes_peer(self):
        before = datetime.now(timezone.utc)
        self._run(self._request())
        self.assertGreaterEqual(self.peer.timestamp, before)

    def test_stale_message_does_not_refresh(self):
        stale_stamp = self.peer.timestamp
        self._run(self._request(timestamp=int(time_module.time()) - 3600))
        self.assertEqual(self.peer.timestamp, stale_stamp)

    def test_handshake_does_not_refresh(self):
        stale_stamp = self.peer.timestamp
        handshake = build_handshake(self.remote, False)
        self._run(handshake)
        self.assertEqual(self.peer.timestamp, stale_stamp)


def _walk_peer(first: int, port: int = 5000) -> Peer:
    return Peer(
        node_secret_key=X25519PrivateKey.generate(),
        peer_public_key=X25519PrivateKey.generate().public_key(),
        storage_public_key_bytes=_key(first),
        address=("10.0.0.1", port + first),
    )


def _walk_node(rounds=8, interval=INTERVAL):
    return SimpleNamespace(
        storage_public_key_bytes=OWN_KEY,
        config=NodeConfig(
            self_lookup_rounds=rounds,
            self_lookup_interval=interval,
            peer_timeout_interval=TIMEOUT_INTERVAL,
        ),
        peer_route=Route(OWN_KEY),
        outgoing_queue=queue.Queue(),
        logger=SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None),
    )


def _requests(node):
    count = 0
    while not node.outgoing_queue.empty():
        node.outgoing_queue.get_nowait()
        count += 1
    return count


class TestTimerWalk(unittest.TestCase):
    def _joined(self, interval=INTERVAL):
        """Return a node and a timer whose join walk finished at now=110."""
        node = _walk_node(interval=interval)
        peer = _walk_peer(0x40)
        node.peer_route.add_peer(peer.public_key_bytes, peer)
        timer = SelfLookupTimer()
        self.assertFalse(self_lookup_timer_step(node, timer, 100.0))
        self.assertEqual(_requests(node), 1)
        self.assertTrue(self_lookup_timer_step(node, timer, 100.0 + TIMEOUT_INTERVAL))
        return node, timer

    def test_timer_joined_after_first_walk(self):
        node = _walk_node()
        peer = _walk_peer(0x40)
        node.peer_route.add_peer(peer.public_key_bytes, peer)
        timer = SelfLookupTimer()
        self.assertFalse(self_lookup_timer_step(node, timer, 100.0))
        self.assertFalse(self_lookup_timer_step(node, timer, 105.0))
        self.assertTrue(self_lookup_timer_step(node, timer, 110.0))
        self.assertTrue(timer.joined)

    def test_completed_counts_each_walk(self):
        node, timer = self._joined()
        self.assertEqual(timer.completed, 1)
        done_at = 100.0 + TIMEOUT_INTERVAL
        self_lookup_timer_step(node, timer, done_at + INTERVAL)
        self.assertEqual(timer.completed, 1)
        self_lookup_timer_step(node, timer, done_at + INTERVAL + TIMEOUT_INTERVAL)
        self.assertEqual(timer.completed, 2)

    def test_completed_unchanged_while_open(self):
        node = _walk_node()
        peer = _walk_peer(0x40)
        node.peer_route.add_peer(peer.public_key_bytes, peer)
        timer = SelfLookupTimer()
        self_lookup_timer_step(node, timer, 100.0)
        self_lookup_timer_step(node, timer, 105.0)
        self.assertEqual(timer.completed, 0)

    def test_walk_restarts_after_interval(self):
        node, timer = self._joined()
        _requests(node)
        done_at = 100.0 + TIMEOUT_INTERVAL
        self.assertEqual(timer.next_walk_at, done_at + INTERVAL)
        self.assertTrue(self_lookup_timer_step(node, timer, done_at + INTERVAL))
        self.assertEqual(_requests(node), 1)
        self.assertFalse(timer.walk.done)
        self.assertEqual(timer.walk.rounds, 1)

    def test_walk_does_not_restart_before_interval(self):
        node, timer = self._joined()
        _requests(node)
        self_lookup_timer_step(node, timer, 110.0 + INTERVAL - 1)
        self.assertEqual(_requests(node), 0)

    def test_joined_stays_true_during_timer_walk(self):
        node, timer = self._joined()
        self.assertTrue(self_lookup_timer_step(node, timer, 110.0 + INTERVAL))
        self.assertFalse(timer.walk.done)
        self.assertTrue(self_lookup_timer_step(node, timer, 111.0 + INTERVAL))
        self.assertTrue(timer.joined)

    def test_zero_interval_disables_timer(self):
        node, timer = self._joined(interval=0)
        _requests(node)
        self.assertIsNone(timer.next_walk_at)
        self_lookup_timer_step(node, timer, 100000.0)
        self.assertEqual(_requests(node), 0)
        self.assertTrue(timer.walk.done)

    def test_zero_rounds_sends_nothing(self):
        node = _walk_node(rounds=0)
        peer = _walk_peer(0x40)
        node.peer_route.add_peer(peer.public_key_bytes, peer)
        timer = SelfLookupTimer()
        self.assertTrue(self_lookup_timer_step(node, timer, 0.0))
        self.assertEqual(_requests(node), 0)

    def test_done_walk_marks_joined(self):
        node = _walk_node()
        timer = SelfLookupTimer(walk=SelfLookup(done=True))
        self.assertTrue(self_lookup_timer_step(node, timer, 0.0))
        self.assertTrue(timer.joined)
        self.assertEqual(_requests(node), 0)

    def test_index_request_not_repeated(self):
        from astreum.storage.workers.advertisements import _index_handoff_step
        from tests.communication.test_storage_index_request import (
            REQUESTER_KEY,
            _node as index_node,
            _peer as index_peer,
        )

        node = index_node(peers=[index_peer(REQUESTER_KEY, 9001)])
        timer = SelfLookupTimer(walk=SelfLookup(done=True), next_walk_at=0.0)
        _index_handoff_step(node, timer)
        self.assertTrue(node.index_requested)
        self.assertEqual(_requests(node), 1)
        # A later step starts a timer walk. It must not send the request again.
        timer.walk = SelfLookup(done=True)
        timer.next_walk_at = 0.0
        _index_handoff_step(node, timer)
        timer.walk = SelfLookup(done=True)
        _index_handoff_step(node, timer)
        self.assertEqual(_requests(node), 0)

    def test_config_default_interval(self):
        config = config_setup({"default_seed": None})
        self.assertEqual(config.self_lookup_interval, DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS)
        self.assertEqual(DEFAULT_SELF_LOOKUP_INTERVAL_SECONDS, 900)

    def test_negative_interval_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "self_lookup_interval": -1})

    def test_non_integer_interval_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "self_lookup_interval": "soon"})

    def test_zero_interval_accepted(self):
        config = config_setup({"default_seed": None, "self_lookup_interval": 0})
        self.assertEqual(config.self_lookup_interval, 0)


class TestKeepAlive(unittest.TestCase):
    def setUp(self):
        self.node = _full_node()
        self.remote = _full_node()
        self.addr = ("10.0.0.5", 5000)
        handle_handshake(
            self.node,
            self.addr,
            build_handshake(self.remote, False, now=int(time_module.time()) - 10),
        )
        _drain(self.node)
        self.key = self.remote.storage_public_key_bytes
        self.peer = self.node.peers[self.key]
        self.peer.timestamp = _old()
        self.probed = {}

    def test_stale_peer_gets_handshake(self):
        _manage_peer_pass(self.node, self.probed, 100.0)
        sent = _drain(self.node)
        self.assertEqual(len(sent), 1)
        message, address = sent[0]
        self.assertEqual(address, self.addr)
        self.assertTrue(message.handshake)
        result = parse_handshake(message)
        self.assertIsNotNone(result)
        self.assertTrue(verify_handshake(message, result, time_module.time(), 60))
        self.assertFalse(result.reply_requested)
        self.assertIn(self.key, self.node.peers)
        self.assertEqual(self.probed, {self.key: 100.0})

    def test_fresh_peer_not_probed(self):
        self.peer.timestamp = datetime.now(timezone.utc)
        _manage_peer_pass(self.node, self.probed, 100.0)
        self.assertEqual(_drain(self.node), [])
        self.assertEqual(self.probed, {})

    def test_answering_peer_kept(self):
        _manage_peer_pass(self.node, self.probed, 100.0)
        _drain(self.node)
        self.peer.timestamp = datetime.now(timezone.utc)
        _manage_peer_pass(self.node, self.probed, 100.0 + 2 * TIMEOUT_INTERVAL)
        self.assertIn(self.key, self.node.peers)
        self.assertNotIn(self.key, self.probed)
        self.assertEqual(_drain(self.node), [])

    def test_silent_peer_kept_before_wait(self):
        _manage_peer_pass(self.node, self.probed, 100.0)
        _drain(self.node)
        _manage_peer_pass(self.node, self.probed, 100.0 + TIMEOUT_INTERVAL)
        self.assertIn(self.key, self.node.peers)
        self.assertEqual(_drain(self.node), [])

    def test_silent_peer_removed(self):
        _manage_peer_pass(self.node, self.probed, 100.0)
        _drain(self.node)
        _manage_peer_pass(self.node, self.probed, 100.0 + 2 * TIMEOUT_INTERVAL)
        self.assertNotIn(self.key, self.node.peers)
        self.assertNotIn(self.key, self.node.peer_route.peers)
        self.assertNotIn(self.key, self.probed)
        self.assertEqual(_drain(self.node), [])

    def test_stale_peer_without_address_removed(self):
        self.peer.address = None
        _manage_peer_pass(self.node, self.probed, 100.0)
        self.assertNotIn(self.key, self.node.peers)
        self.assertEqual(_drain(self.node), [])
        self.assertEqual(self.probed, {})

    def test_keep_alive_sent_once(self):
        _manage_peer_pass(self.node, self.probed, 100.0)
        _drain(self.node)
        _manage_peer_pass(self.node, self.probed, 105.0)
        self.assertEqual(_drain(self.node), [])

    def test_live_peer_answers_keep_alive(self):
        """The handshake from the pass makes the remote answer with a PING."""
        _manage_peer_pass(self.node, self.probed, 100.0)
        (keep_alive, _address), = _drain(self.node)
        # The remote already knows this node (it handshaked first).
        handle_handshake(
            self.remote,
            self.addr,
            build_handshake(self.node, False, now=int(time_module.time()) - 10),
        )
        _drain(self.remote)
        handle_handshake(self.remote, ("10.0.0.9", 9000), keep_alive)
        sent = _drain(self.remote)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0][0].handshake)


if __name__ == "__main__":
    unittest.main()
