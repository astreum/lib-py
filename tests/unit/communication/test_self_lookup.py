"""Tests for the self-lookup walk and its config key."""
import queue
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer import Peer
from astreum.communication.models.route import Route
from astreum.communication.self_lookup import SelfLookup, self_lookup_step
from astreum.utils.config import DEFAULT_SELF_LOOKUP_ROUNDS, config_setup
from astreum.utils.config import NodeConfig

OWN_KEY = b"\x00" * 32
TIMEOUT = 10


def _key(first: int) -> bytes:
    return bytes([first]) + b"\x00" * 31


def _peer(first: int) -> Peer:
    return Peer(
        node_secret_key=X25519PrivateKey.generate(),
        peer_public_key=X25519PrivateKey.generate().public_key(),
        storage_public_key_bytes=_key(first),
        address=("10.0.0.1", 5000 + first),
    )


def _node(rounds=8):
    return SimpleNamespace(
        storage_public_key_bytes=OWN_KEY,
        config=NodeConfig(self_lookup_rounds=rounds, peer_timeout_interval=TIMEOUT),
        peer_route=Route(OWN_KEY),
        outgoing_queue=queue.Queue(),
        logger=SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None),
    )


def _join(node, peer):
    node.peer_route.add_peer(peer.public_key_bytes, peer)
    return peer


def _sent(node, peer):
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        message = Message.from_bytes(payload[8:])
        message.decrypt(peer.shared_key_bytes)
        out.append((address, message.topic, message.content))
    return out


class TestWalkStep(unittest.TestCase):
    def test_waits_without_peer(self):
        node, walk = _node(), SelfLookup()
        self.assertFalse(self_lookup_step(node, walk, 0.0))
        self.assertTrue(node.outgoing_queue.empty())
        self.assertEqual(walk.rounds, 0)
        self.assertFalse(walk.done)

    def test_first_round_asks_nearest_peer(self):
        node, walk = _node(), SelfLookup()
        _join(node, _peer(0x80))
        near = _join(node, _peer(0x40))
        self.assertFalse(self_lookup_step(node, walk, 100.0))
        self.assertEqual(
            _sent(node, near),
            [(near.address, MessageTopic.ROUTE_REQUEST, b"\x00")],
        )
        self.assertEqual(walk.rounds, 1)
        self.assertEqual(walk.deadline, 100.0 + TIMEOUT)
        self.assertEqual(walk.distance, 0x40 << 248)

    def test_waits_while_no_nearer_peer(self):
        node, walk = _node(), SelfLookup()
        peer = _join(node, _peer(0x40))
        self_lookup_step(node, walk, 100.0)
        _sent(node, peer)
        self.assertFalse(self_lookup_step(node, walk, 105.0))
        self.assertTrue(node.outgoing_queue.empty())
        self.assertFalse(walk.done)

    def test_nearer_peer_starts_round_before_deadline(self):
        node, walk = _node(), SelfLookup()
        _join(node, _peer(0x40))
        self_lookup_step(node, walk, 100.0)
        nearer = _join(node, _peer(0x10))
        node.outgoing_queue = queue.Queue()
        self.assertFalse(self_lookup_step(node, walk, 101.0))
        self.assertEqual([a for a, _, _ in _sent(node, nearer)], [nearer.address])
        self.assertEqual(walk.rounds, 2)
        self.assertEqual(walk.deadline, 101.0 + TIMEOUT)

    def test_no_nearer_peer_completes_walk(self):
        node, walk = _node(), SelfLookup()
        peer = _join(node, _peer(0x40))
        self_lookup_step(node, walk, 100.0)
        _sent(node, peer)
        self.assertTrue(self_lookup_step(node, walk, 100.0 + TIMEOUT))
        self.assertTrue(walk.done)
        self.assertTrue(node.outgoing_queue.empty())

    def test_round_cap_completes_walk(self):
        node, walk = _node(rounds=2), SelfLookup()
        _join(node, _peer(0x40))
        self_lookup_step(node, walk, 100.0)
        _join(node, _peer(0x20))
        self_lookup_step(node, walk, 101.0)
        self.assertEqual(walk.rounds, 2)
        node.outgoing_queue = queue.Queue()
        _join(node, _peer(0x10))
        self.assertTrue(self_lookup_step(node, walk, 102.0))
        self.assertEqual(walk.rounds, 2)
        self.assertTrue(node.outgoing_queue.empty())

    def test_round_cap_waits_for_last_round(self):
        node, walk = _node(rounds=1), SelfLookup()
        _join(node, _peer(0x40))
        self_lookup_step(node, walk, 100.0)
        self.assertFalse(self_lookup_step(node, walk, 101.0))
        self.assertFalse(walk.done)
        self.assertTrue(self_lookup_step(node, walk, 100.0 + TIMEOUT))

    def test_zero_rounds_disables_walk(self):
        node, walk = _node(rounds=0), SelfLookup()
        _join(node, _peer(0x40))
        self.assertTrue(self_lookup_step(node, walk, 0.0))
        self.assertTrue(walk.done)
        self.assertTrue(node.outgoing_queue.empty())

    def test_done_is_final(self):
        node, walk = _node(), SelfLookup(done=True)
        _join(node, _peer(0x40))
        self.assertTrue(self_lookup_step(node, walk, 0.0))
        self.assertTrue(node.outgoing_queue.empty())


class TestWalkConfig(unittest.TestCase):
    def test_config_default_rounds(self):
        config = config_setup({"default_seed": None})
        self.assertEqual(config.self_lookup_rounds, DEFAULT_SELF_LOOKUP_ROUNDS)
        self.assertEqual(DEFAULT_SELF_LOOKUP_ROUNDS, 8)

    def test_config_negative_rounds_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "self_lookup_rounds": -1})

    def test_config_zero_rounds_allowed(self):
        config = config_setup({"default_seed": None, "self_lookup_rounds": 0})
        self.assertEqual(config.self_lookup_rounds, 0)


if __name__ == "__main__":
    unittest.main()
