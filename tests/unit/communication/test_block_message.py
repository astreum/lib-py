"""``BLOCK``: a validator announces its newest block; peers record who holds it.

Blocks here are real: signed, mined, and decoded by the production code.
"""

import sys
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from astreum.communication.handlers import block as block_handler  # noqa: E402
from astreum.communication.handlers import ping as ping_handler  # noqa: E402
from astreum.communication.message_pow import MAX_INLINE_MESSAGE_BYTES  # noqa: E402
from astreum.communication.models.message import Message, MessageTopic  # noqa: E402
from astreum.communication.models.ping import Ping  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.difficulty import calculate_block_difficulty  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.communication.models.relay_endpoint import RelayEndpoint  # noqa: E402
from astreum.consensus.block.holders import (  # noqa: E402
    MAX_BLOCK_HOLDERS,
    block_holder,
    init_block_holders,
    record_block_holder,
)
from astreum.consensus.block.message import (  # noqa: E402
    block_header_exprs,
    decode_block_message,
    encode_block_message,
)
from astreum.consensus.block.nonce import generate_block_nonce  # noqa: E402
from astreum.consensus.block.utils.bits import count_leading_zero_bits  # noqa: E402
from astreum.consensus.validation import worker as validation_worker  # noqa: E402
from astreum.expression import NIL, ZERO32, bytes_, link  # noqa: E402
from astreum.utils.config import NodeConfig

RELAY_KEY = b"\x21" * 32
PROVIDER = b"\x11" * 32 + RELAY_KEY + bytes([10, 0, 0, 9]) + (9000).to_bytes(2, "big")
CONTACT = RelayEndpoint(RELAY_KEY, ("10.0.0.9", 9000))


def assert_is_contact(test, endpoint):
    test.assertIsInstance(endpoint, RelayEndpoint)
    test.assertEqual(endpoint.relay_key, CONTACT.relay_key)
    test.assertEqual(endpoint.address, CONTACT.address)


def _pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _mined_block(previous, key, *, sign=True, pow_ok=True, height=None):
    """A block on *previous*, signed by *key* and mined at difficulty 1."""
    timestamp = previous.timestamp + 2
    block = create_block(
        chain_id=0,
        previous_block_hash=previous.expr_id,
        previous_block=previous,
        height=previous.height + 1 if height is None else height,
        timestamp=timestamp,
        accounts_hash=b"\x0a" * 32,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, 1, 0, 0)],
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=calculate_block_difficulty(previous.timestamp, timestamp, previous.difficulty),
        validator_public_key_bytes=_pub(key),
    )
    signer = key.sign if sign else (lambda body_hash: b"\x00" * 64)
    nonce = 0
    while True:
        block.nonce = nonce
        block._expr = None
        get_block_expr(block)
        block.signature = signer(block.body_hash)
        block._expr = None
        block_hash = get_block_expr(block).hash()
        if (count_leading_zero_bits(block_hash) >= 1) == pow_ok:
            block.expr_id = block_hash
            return block
        nonce += 1


def _genesis_like(key):
    block = create_block(
        chain_id=0,
        previous_block_hash=ZERO32,
        previous_block=None,
        height=0,
        timestamp=100,
        accounts_hash=b"\x0a" * 32,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, 1, 0, 0)],
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=1,
        validator_public_key_bytes=_pub(key),
    )
    generate_block_nonce(block=block, difficulty=1, sign=key.sign)
    return block


def _node(latest, *, validator=False):
    node = SimpleNamespace(
        logger=MagicMock(),
        latest_block_lock=threading.RLock(),
        latest_block_hash=latest.expr_id,
        latest_block=latest,
        config=NodeConfig(
            chain_id=0,
            validation_secret_key=object() if validator else None,
            hot_storage_limit=64 * 1024 * 1024,
            cold_storage_path=None,
        ),
        hot_storage={},
        hot_storage_timestamps={},
        hot_storage_lock=threading.RLock(),
        hot_storage_size=0,
        is_connected=False,
        storage_public_key_bytes=b"\x31" * 32,
    )
    init_block_holders(node)
    return node


def _peer(tag: int) -> SimpleNamespace:
    return SimpleNamespace(
        address=(f"10.0.1.{tag}", 5000 + tag),
        public_key_bytes=bytes([tag]) * 32,
        shared_key_bytes=bytes([tag]) * 32,
        difficulty=1,
    )


class _Relay:
    """Capture what the handler forwards and to whom."""

    def __init__(self, validators):
        self.validators = validators
        self.sent = []

    def __enter__(self):
        def fake_enqueue(node, address, message, difficulty=1):
            self.sent.append((address, message))
            return True

        self._patches = [
            patch.object(block_handler, "validator_peers", return_value=self.validators),
            patch.object(block_handler, "enqueue_outgoing", side_effect=fake_enqueue),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    @property
    def addresses(self):
        return [address for address, _ in self.sent]


class _Chain(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.genesis = _genesis_like(self.key)
        self.block = _mined_block(self.genesis, self.key)
        self.block_hash = get_block_expr(self.block).hash()
        self.payload = encode_block_message(PROVIDER, get_block_expr(self.block))


class TestBlockCodec(_Chain):
    def test_round_trip_keeps_the_block_hash_and_contact(self):
        contact, exprs = decode_block_message(self.payload)
        assert_is_contact(self, contact)
        self.assertEqual(exprs[0].hash(), self.block_hash)

    def test_fits_one_datagram(self):
        self.assertLessEqual(len(self.payload), MAX_INLINE_MESSAGE_BYTES)

    def test_header_exprs_are_in_memory_only_and_unique(self):
        exprs = block_header_exprs(get_block_expr(self.block))
        hashes = [e.hash() for e in exprs]
        self.assertEqual(hashes[0], self.block_hash)
        self.assertEqual(len(hashes), len(set(hashes)))
        # Hash references (accounts, receipts, ...) are not expanded.
        self.assertNotIn(self.block.accounts_hash, hashes)

    def test_decoded_header_loads_as_the_same_block(self):
        _, exprs = decode_block_message(self.payload)
        scratch = block_handler._scratch_node(SimpleNamespace(logger=MagicMock()))
        for e in exprs:
            block_handler.put_expr_in_hot_storage(scratch, e)
        loaded = get_block_from_storage(scratch, self.block_hash, load_bloom=False)
        self.assertEqual(loaded.height, self.block.height)
        self.assertEqual(loaded.timestamp, self.block.timestamp)
        self.assertEqual(loaded.validator_public_key_bytes, self.block.validator_public_key_bytes)
        self.assertEqual(loaded.signature, self.block.signature)

    def test_rejects_a_bad_provider_payload(self):
        with self.assertRaises(ValueError):
            encode_block_message(b"\x00" * 10, get_block_expr(self.block))

    def test_garbage_does_not_decode(self):
        self.assertIsNone(decode_block_message(b""))
        self.assertIsNone(decode_block_message(b"\x00" * 70))
        self.assertIsNone(decode_block_message(PROVIDER + b"\x01\x00"))


class TestBlockHolders(unittest.TestCase):
    def test_keeps_the_first_holder_and_is_bounded(self):
        node = SimpleNamespace()
        init_block_holders(node)
        self.assertTrue(record_block_holder(node, b"a" * 32, CONTACT))
        self.assertFalse(record_block_holder(node, b"a" * 32, RelayEndpoint(b"\x00" * 32, ("1.1.1.1", 1))))
        self.assertEqual(block_holder(node, b"a" * 32), CONTACT)
        for i in range(MAX_BLOCK_HOLDERS * 2):
            record_block_holder(node, bytes([i]) * 32, CONTACT)
        self.assertEqual(len(node.block_holders), MAX_BLOCK_HOLDERS)
        self.assertIsNone(block_holder(node, b"a" * 32))

    def test_unknown_or_missing_state(self):
        self.assertIsNone(block_holder(SimpleNamespace(), b"x" * 32))
        node = SimpleNamespace()
        init_block_holders(node)
        self.assertIsNone(block_holder(node, b"x" * 32))
        self.assertIsNone(block_holder(node, None))


class TestBlockHandler(_Chain):
    def _handle(self, node, payload=None, sender=None, validators=None):
        sender = sender or _peer(1)
        with _Relay(validators if validators is not None else {}) as relay:
            block_handler.handle_block(node, sender, payload or self.payload)
        return relay

    def _untouched(self, node, relay):
        self.assertEqual(node.block_holders, OrderedDict())
        self.assertEqual(node.hot_storage, {})
        self.assertEqual(node.latest_block_hash, self.genesis.expr_id)
        self.assertEqual(relay.sent, [])

    def test_valid_block_records_the_holder_and_stores_the_header(self):
        node = _node(self.genesis)
        relay = self._handle(node)
        assert_is_contact(self, block_holder(node, self.block_hash))
        loaded = get_block_from_storage(node, self.block_hash, load_bloom=False)
        self.assertEqual(loaded.height, 1)
        self.assertEqual(relay.sent, [])

    def test_non_validator_does_not_adopt_or_forward(self):
        node = _node(self.genesis)
        relay = self._handle(node, validators={_peer(2).public_key_bytes: _peer(2)})
        self.assertEqual(node.latest_block_hash, self.genesis.expr_id)
        self.assertEqual(relay.sent, [])

    def test_validator_adopts_the_block(self):
        node = _node(self.genesis, validator=True)
        self._handle(node)
        self.assertEqual(node.latest_block_hash, self.block_hash)
        self.assertEqual(node.latest_block.expr_id, self.block_hash)
        assert_is_contact(self, node.latest_block.contact)

    def test_validator_forwards_to_validators_but_not_back_to_the_sender(self):
        node = _node(self.genesis, validator=True)
        sender, other, third = _peer(1), _peer(2), _peer(3)
        validators = {p.public_key_bytes: p for p in (sender, other, third)}
        relay = self._handle(node, sender=sender, validators=validators)
        self.assertEqual(sorted(relay.addresses), sorted([other.address, third.address]))
        for _, message in relay.sent:
            self.assertEqual(message.topic, MessageTopic.BLOCK)
            self.assertEqual(message.content, self.payload)

    def test_relayed_block_records_the_validator_not_the_forwarder(self):
        node = _node(self.genesis)
        forwarder = _peer(7)
        self._handle(node, sender=forwarder)
        assert_is_contact(self, block_holder(node, self.block_hash))
        self.assertNotEqual(block_holder(node, self.block_hash).address[0], forwarder.address[0])

    def test_a_block_seen_before_is_not_recorded_or_forwarded_again(self):
        node = _node(self.genesis, validator=True)
        validators = {_peer(2).public_key_bytes: _peer(2)}
        first = self._handle(node, validators=validators)
        self.assertEqual(len(first.sent), 1)
        again = self._handle(node, validators=validators)
        self.assertEqual(again.sent, [])
        self.assertEqual(len(node.block_holders), 1)

    def test_invalid_signature_is_dropped(self):
        bad = _mined_block(self.genesis, self.key, sign=False)
        node = _node(self.genesis, validator=True)
        relay = self._handle(
            node,
            payload=encode_block_message(PROVIDER, get_block_expr(bad)),
            validators={_peer(2).public_key_bytes: _peer(2)},
        )
        self._untouched(node, relay)

    def test_failed_proof_of_work_is_dropped(self):
        weak = _mined_block(self.genesis, self.key, pow_ok=False)
        node = _node(self.genesis, validator=True)
        relay = self._handle(
            node,
            payload=encode_block_message(PROVIDER, get_block_expr(weak)),
            validators={_peer(2).public_key_bytes: _peer(2)},
        )
        self._untouched(node, relay)

    def test_block_not_built_on_our_latest_is_dropped(self):
        other_key = Ed25519PrivateKey.generate()
        other_genesis = _genesis_like(other_key)
        node = _node(other_genesis, validator=True)
        relay = self._handle(node, validators={_peer(2).public_key_bytes: _peer(2)})
        self.assertEqual(node.block_holders, OrderedDict())
        self.assertEqual(node.hot_storage, {})
        self.assertEqual(node.latest_block_hash, other_genesis.expr_id)
        self.assertEqual(relay.sent, [])

    def test_node_without_a_latest_block_cannot_verify_and_drops(self):
        node = _node(self.genesis, validator=True)
        node.latest_block = None
        relay = self._handle(node, validators={_peer(2).public_key_bytes: _peer(2)})
        self.assertEqual(node.block_holders, OrderedDict())
        self.assertEqual(relay.sent, [])

    def test_malformed_payload_is_dropped(self):
        node = _node(self.genesis, validator=True)
        relay = self._handle(node, payload=b"\x01" * 100)
        self._untouched(node, relay)


class TestPingNoLongerRecordsAHolder(unittest.TestCase):
    def test_validator_ping_does_not_request_or_record_anything(self):
        node = SimpleNamespace(
            logger=MagicMock(),
            latest_block_lock=threading.RLock(),
            latest_block_hash=None,
            latest_block=None,
            validation_route=None,
        )
        init_block_holders(node)
        peer = SimpleNamespace(
            timestamp=None,
            latest_block=None,
            difficulty=1,
            is_default_seed=False,
            address=("10.0.0.5", 5000),
            public_key_bytes=b"\x06" * 32,
        )
        payload = Ping(is_validator=True, difficulty=1, latest_block=b"\x07" * 32).to_bytes()
        with patch("astreum.storage.workers.requests.queue_storage_get") as queue_get:
            ping_handler.handle_ping(node, peer, payload)
        queue_get.assert_not_called()
        self.assertEqual(node.block_holders, OrderedDict())


class TestWorkerAnnounce(_Chain):
    def _node(self, validators):
        node = _node(self.genesis, validator=True)
        return node, validators

    def test_announces_the_block_to_every_validator(self):
        node, _ = self._node({})
        validators = {_peer(i).public_key_bytes: _peer(i) for i in (2, 3)}
        sent = []
        with patch.object(validation_worker, "validator_peers", return_value=validators), \
             patch.object(validation_worker, "enqueue_outgoing",
                          side_effect=lambda n, a, message, difficulty=1: sent.append((a, message)) or True), \
             patch("astreum.storage.exprs.network.build_provider_payload", return_value=PROVIDER):
            validation_worker._announce_block(node, self.block)
        self.assertEqual(sorted(a for a, _ in sent), sorted(p.address for p in validators.values()))
        for _, message in sent:
            self.assertEqual(message.topic, MessageTopic.BLOCK)
            contact, exprs = decode_block_message(message.content)
            assert_is_contact(self, contact)
            self.assertEqual(exprs[0].hash(), self.block_hash)

    def test_a_failure_building_the_message_is_logged_not_raised(self):
        node, _ = self._node({})
        with patch("astreum.storage.exprs.network.build_provider_payload", side_effect=KeyError("x")):
            validation_worker._announce_block(node, self.block)
        node.logger.error.assert_called_once()

    def test_included_transactions_are_collected_once_without_fetching(self):
        shared = bytes_(b"shared")
        tx_a = SimpleNamespace(hash=b"a" * 32, expr=lambda: link(shared, link(bytes_(b"a"), NIL)))
        tx_b = SimpleNamespace(hash=b"b" * 32, expr=lambda: link(shared, link(bytes_(b"b"), NIL)))
        node = SimpleNamespace(logger=MagicMock())
        exprs = validation_worker.included_transaction_exprs(node, [tx_a, tx_b])
        hashes = [e.hash() for e in exprs]
        self.assertEqual(len(hashes), len(set(hashes)))
        self.assertIn(shared.hash(), hashes)
        self.assertEqual(validation_worker.included_transaction_exprs(node, []), [])


if __name__ == "__main__":
    unittest.main()
