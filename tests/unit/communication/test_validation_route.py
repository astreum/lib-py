"""Every node keeps a validator list, with or without a validation key."""
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from astreum.communication.handlers.ping import handle_ping
from astreum.communication.handlers.route_response import handle_route_response
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.peer_info import PeerInfo, encode_peer_info
from astreum.communication.setup import make_routes
from astreum.consensus.validation.peers import validator_peers
from tests.unit.communication.test_route_reply import (
    OWN_KEY,
    SENDER_KEY,
    _add,
    _entry_keys,
    _key,
    _node,
    _peer,
    _reply,
    _request,
)


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _ping_node():
    node = _node()
    node.latest_block_lock = threading.RLock()
    node.latest_block_hash = None
    node.latest_block = None
    return node


def _ping(is_validator: bool) -> bytes:
    from astreum.communication.models.ping import Ping

    return Ping(is_validator=is_validator, difficulty=1, latest_block=None).to_bytes()


class TestMakeRoutes(unittest.TestCase):
    def test_node_without_key_has_a_validation_route_at_the_storage_key(self):
        peer_route, validation_route = make_routes(OWN_KEY, None)
        self.assertIsNotNone(validation_route)
        self.assertEqual(validation_route.storage_public_key_bytes, OWN_KEY)
        self.assertIsNot(peer_route, validation_route)

    def test_node_with_key_has_a_validation_route_at_the_validation_key(self):
        key = Ed25519PrivateKey.generate()
        _, validation_route = make_routes(OWN_KEY, key)
        self.assertEqual(validation_route.storage_public_key_bytes, _public(key))


class TestPingOnPlainNode(unittest.TestCase):
    def test_ping_from_validator_adds_it_to_validator_peers(self):
        node = _ping_node()
        validator = _add(node, _peer(_key(0x10)))
        handle_ping(node, validator, _ping(True))
        self.assertIn(validator.public_key_bytes, validator_peers(node))

    def test_ping_without_validator_flag_removes_it(self):
        node = _ping_node()
        peer = _add(node, _peer(_key(0x10)))
        handle_ping(node, peer, _ping(True))
        handle_ping(node, peer, _ping(False))
        self.assertEqual(validator_peers(node), {})

    def test_ping_from_plain_peer_is_not_a_validator(self):
        node = _ping_node()
        peer = _add(node, _peer(_key(0x10)))
        handle_ping(node, peer, _ping(False))
        self.assertEqual(validator_peers(node), {})


class TestRouteIdOneOnPlainNode(unittest.TestCase):
    def test_plain_node_answers_route_request_id_1_with_its_validators(self):
        node = _ping_node()
        requester = _add(node, _peer(SENDER_KEY))
        validator = _add(node, _peer(_key(0x10)))
        handle_ping(node, validator, _ping(True))
        _add(node, _peer(_key(0x20)))  # a plain peer: not listed

        self.assertTrue(_request(node, requester, route_id=1)[0])
        content = _reply(node, requester)
        self.assertEqual(content[0], 1)
        self.assertEqual(_entry_keys(content), [_key(0x10)])

    def test_plain_node_with_no_validators_answers_with_an_empty_list(self):
        node = _ping_node()
        requester = _add(node, _peer(SENDER_KEY))
        self.assertTrue(_request(node, requester, route_id=1)[0])
        self.assertEqual(_entry_keys(_reply(node, requester)), [])

    def test_plain_node_handshakes_validators_from_route_response(self):
        node = _ping_node()
        node.storage_public_key_bytes = OWN_KEY
        node.use_ipv6 = False
        node.config = SimpleNamespace(validation_public_key_bytes=None)
        node.latest_block_hash = None
        node.latest_block = None
        node.relay_secret_key = None
        seed = _add(node, _peer(SENDER_KEY))
        unknown = _peer(_key(0x30), "10.0.0.7", 7000)
        content = bytes([1]) + encode_peer_info(
            PeerInfo(unknown.public_key_bytes, unknown.relay_public_key_bytes, unknown.address),
            False,
        )
        message = Message(
            topic=MessageTopic.ROUTE_RESPONSE,
            content=content,
            sender_public_key_bytes=seed.public_key_bytes,
        )
        from unittest import mock

        with mock.patch(
            "astreum.communication.handlers.route_response.build_handshake",
            return_value=object(),
        ), mock.patch(
            "astreum.communication.handlers.route_response.enqueue_outgoing"
        ) as enqueue:
            handle_route_response(node, seed, message)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[1], ("10.0.0.7", 7000))


class TestNodeKeepsRoute(unittest.TestCase):
    def test_validate_blockchain_keeps_the_same_route(self):
        import socket

        from astreum.communication.disconnect import disconnect_node
        from astreum.communication.node import connect_node
        from astreum.consensus.validation.node import validate_blockchain
        from astreum.node import Node

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        directory = tempfile.TemporaryDirectory(prefix="astreum_route_")
        self.addCleanup(directory.cleanup)
        node = Node(config={
            "port": port,
            "default_seed": None,
            "additional_seeds": [],
            "cold_storage_path": directory.name,
            "cold_storage_scale": "KB",
            "logging_enabled": False,
            "verbose": False,
        })
        connect_node(node)
        try:
            route = node.validation_route
            self.assertIsNotNone(route)
            self.assertEqual(route.storage_public_key_bytes, node.storage_public_key_bytes)
            validate_blockchain(node, Ed25519PrivateKey.generate())
            self.assertIs(node.validation_route, route)
        finally:
            disconnect_node(node)


class TestKeyBeforeConnect(unittest.TestCase):
    def test_validate_blockchain_sets_the_key_before_it_connects(self):
        from unittest import mock

        from astreum.consensus.validation import node as validation_node

        key = Ed25519PrivateKey.generate()
        seen = {}
        node = SimpleNamespace(logger=mock.Mock(), config=SimpleNamespace(
            validation_secret_key=None,
            validation_public_key_bytes=None,
            default_seed=None,
        ))

        def connect(target):
            seen["secret"] = target.config.validation_secret_key
            seen["public"] = target.config.validation_public_key_bytes
            raise RuntimeError("stop after connect")

        with mock.patch.object(validation_node, "connect_node", connect):
            with self.assertRaises(RuntimeError):
                validation_node.validate_blockchain(node, key)
        self.assertIs(seen["secret"], key)
        self.assertEqual(seen["public"], _public(key))


if __name__ == "__main__":
    unittest.main()
