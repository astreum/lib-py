"""A ping carries the holder of the head block."""
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from astreum.communication.handlers.ping import handle_ping
from astreum.communication.models.ping import Ping
from astreum.communication.models.relay_endpoint import RelayEndpoint
from astreum.consensus.block.holders import (
    block_holder,
    get_ping_holder,
    init_block_holders,
)
from tests.unit.communication.test_route_reply import _add, _node, _peer, _key

HEAD = b"\x07" * 32
OTHER = b"\x08" * 32
OWN_VALIDATION_KEY = b"\x21" * 32
HOLDER = RelayEndpoint(b"\x09" * 32, ("10.1.2.3", 52780))
OTHER_HOLDER = RelayEndpoint(b"\x0a" * 32, ("10.1.2.4", 52781))


def _plain_node(head_hash=None, head=None, secret_key=None):
    node = _node()
    node.latest_block_lock = threading.Lock()
    node.latest_block_hash = head_hash
    node.latest_block = head
    node.config = SimpleNamespace(
        validation_secret_key=secret_key,
        validation_public_key_bytes=OWN_VALIDATION_KEY if secret_key else None,
        relay_public_key_bytes=b"\x55" * 32,
        port=52780,
    )
    node.relay_ip_address = "10.9.9.9"
    init_block_holders(node)
    return node


def _ping(latest=HEAD, holder=HOLDER, is_validator=True) -> bytes:
    return Ping(is_validator, 1, latest, holder).to_bytes()


def _sender(node):
    peer = _add(node, _peer(_key(0x10)))
    peer.is_default_seed = False
    return peer


class TestHandlePing(unittest.TestCase):
    def test_plain_node_records_holder_from_validator_ping(self):
        node = _plain_node()
        handle_ping(node, _sender(node), _ping())
        self.assertEqual(block_holder(node, HEAD).address, HOLDER.address)
        self.assertEqual(block_holder(node, HEAD).relay_key, HOLDER.relay_key)

    def test_ping_from_non_validator_records_nothing(self):
        node = _plain_node()
        handle_ping(node, _sender(node), _ping(is_validator=False))
        self.assertIsNone(block_holder(node, HEAD))

    def test_ping_without_holder_records_nothing(self):
        node = _plain_node()
        handle_ping(node, _sender(node), _ping(holder=None))
        self.assertIsNone(block_holder(node, HEAD))

    def test_second_ping_keeps_first_holder(self):
        node = _plain_node()
        peer = _sender(node)
        handle_ping(node, peer, _ping())
        handle_ping(node, peer, _ping(holder=OTHER_HOLDER))
        self.assertEqual(block_holder(node, HEAD).address, HOLDER.address)

    def test_validator_node_ignores_ping_holder(self):
        node = _plain_node(secret_key=object())
        handle_ping(node, _sender(node), _ping())
        self.assertIsNone(block_holder(node, HEAD))

    def test_ping_holder_for_current_head_keeps_the_head(self):
        block = object()
        node = _plain_node(HEAD, block)
        handle_ping(node, _sender(node), _ping())
        self.assertEqual(node.latest_block_hash, HEAD)
        self.assertIs(node.latest_block, block)
        self.assertIsNotNone(block_holder(node, HEAD))

    def test_ping_holder_for_other_hash_keeps_the_head(self):
        block = object()
        node = _plain_node(HEAD, block)
        handle_ping(node, _sender(node), _ping(latest=OTHER))
        self.assertEqual(node.latest_block_hash, HEAD)
        self.assertIs(node.latest_block, block)
        self.assertIsNotNone(block_holder(node, OTHER))
        self.assertIsNone(block_holder(node, HEAD))

    def test_default_seed_ping_with_same_hash_keeps_latest_block(self):
        block = object()
        node = _plain_node(HEAD, block)
        peer = _sender(node)
        peer.is_default_seed = True
        handle_ping(node, peer, _ping())
        self.assertEqual(node.latest_block_hash, HEAD)
        self.assertIs(node.latest_block, block)


class TestGetPingHolder(unittest.TestCase):
    def test_creator_returns_own_contact(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block, secret_key=object())
        holder = get_ping_holder(node, HEAD, block)
        self.assertEqual(holder.relay_key, b"\x55" * 32)
        self.assertEqual(holder.address, ("10.9.9.9", 52780))

    def test_creator_is_not_recorded(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block, secret_key=object())
        get_ping_holder(node, HEAD, block)
        self.assertIsNone(block_holder(node, HEAD))
        self.assertEqual(len(node.block_holders), 0)

    def test_adopter_returns_creator_contact(self):
        block = SimpleNamespace(validator_public_key_bytes=b"\x33" * 32)
        node = _plain_node(HEAD, block, secret_key=object())
        node.block_holders[HEAD] = HOLDER
        self.assertIs(get_ping_holder(node, HEAD, block), HOLDER)

    def test_recorded_holder_wins_over_own_contact(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block, secret_key=object())
        node.block_holders[HEAD] = HOLDER
        self.assertIs(get_ping_holder(node, HEAD, block), HOLDER)

    def test_no_holder_returns_none(self):
        block = SimpleNamespace(validator_public_key_bytes=b"\x33" * 32)
        node = _plain_node(HEAD, block, secret_key=object())
        self.assertIsNone(get_ping_holder(node, HEAD, block))

    def test_block_not_loaded_returns_none(self):
        node = _plain_node(HEAD, None, secret_key=object())
        self.assertIsNone(get_ping_holder(node, HEAD, None))

    def test_no_head_returns_none(self):
        node = _plain_node()
        self.assertIsNone(get_ping_holder(node, None, None))

    def test_node_without_validation_key_returns_none_for_unknown_holder(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block)
        self.assertIsNone(get_ping_holder(node, HEAD, block))

    def test_no_relay_address_returns_none(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block, secret_key=object())
        node.relay_ip_address = None
        self.assertIsNone(get_ping_holder(node, HEAD, block))

    def test_ping_built_from_the_holder_encodes(self):
        block = SimpleNamespace(validator_public_key_bytes=OWN_VALIDATION_KEY)
        node = _plain_node(HEAD, block, secret_key=object())
        holder = get_ping_holder(node, HEAD, block)
        ping = Ping.from_bytes(Ping(True, 1, HEAD, holder).to_bytes())
        self.assertEqual(ping.holder.address, ("10.9.9.9", 52780))


if __name__ == "__main__":
    unittest.main()
