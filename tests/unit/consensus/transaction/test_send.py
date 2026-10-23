"""send_transaction: who gets the transaction."""

from __future__ import annotations

import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.route import Route
from astreum.consensus.transaction import Transaction, TransactionCode
from astreum.consensus.transaction import send as send_module
from astreum.consensus.transaction.send import send_transaction
from tests.unit.communication.test_route_reply import OWN_KEY, _add, _key, _peer


def _signed() -> Transaction:
    key = Ed25519PrivateKey.generate()
    tx = Transaction(
        chain_id=0,
        amount=5,
        code=TransactionCode.TRANSFER,
        counter=0,
        recipient=os.urandom(32),
        sender=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    tx.sign(key)
    return tx


def _node(*, validator: bool):
    return SimpleNamespace(
        is_connected=True,
        latest_block=object(),
        logger=mock.Mock(),
        storage_public_key_bytes=OWN_KEY,
        use_ipv6=False,
        peers={},
        peers_lock=threading.RLock(),
        peer_route=Route(OWN_KEY),
        validation_route=Route(OWN_KEY),
        config=SimpleNamespace(
            validation_secret_key=Ed25519PrivateKey.generate() if validator else None,
            peer_timeout_interval=0.2,
        ),
        _validation_transaction_queue=queue.Queue(),
    )


def _validator_peer(node, key: bytes):
    peer = _add(node, _peer(key), route=node.validation_route)
    return peer


class TestSendTransaction(unittest.TestCase):
    def setUp(self) -> None:
        self.sent: list[tuple[tuple, Message]] = []

        def capture(node, address, *, message, difficulty):
            self.sent.append((address, message))
            return True

        patches = [
            mock.patch.object(send_module, "enqueue_outgoing", capture),
            mock.patch.object(send_module, "put_expr_in_hot_storage", return_value=True),
            mock.patch.object(send_module, "put_expr_in_cold_storage"),
            mock.patch.object(send_module, "resolve_inner_exprs", return_value=([], [])),
            mock.patch(
                "astreum.consensus.transaction.message.encode_transaction_message",
                return_value=b"payload",
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _topics(self, topic: MessageTopic) -> list[tuple]:
        return [address for address, message in self.sent if message.topic == topic]

    def test_sends_to_a_validator_known_only_from_pings(self) -> None:
        # The ping handler files a validator in the buckets, with no Peer
        # object in ``validation_route.peers``.
        node = _node(validator=False)
        peer = _validator_peer(node, _key(0x10))
        node.validation_route.peers.clear()
        node.validation_route.add_peer(peer.public_key_bytes)

        send_transaction(node, _signed())

        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [peer.address])
        self.assertEqual(self._topics(MessageTopic.ROUTE_REQUEST), [])

    def test_sends_to_every_known_validator(self) -> None:
        node = _node(validator=False)
        first = _validator_peer(node, _key(0x10))
        second = _validator_peer(node, _key(0x20))
        send_transaction(node, _signed())
        self.assertCountEqual(
            self._topics(MessageTopic.TRANSACTION), [first.address, second.address]
        )

    def test_plain_peer_is_not_sent_the_transaction(self) -> None:
        node = _node(validator=False)
        _add(node, _peer(_key(0x30)))  # in the peer route only
        validator = _validator_peer(node, _key(0x10))
        send_transaction(node, _signed())
        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [validator.address])

    def test_no_validator_asks_every_peer_then_raises(self) -> None:
        node = _node(validator=False)
        peer = _add(node, _peer(_key(0x30)))
        with self.assertRaisesRegex(RuntimeError, "no validator available"):
            send_transaction(node, _signed())
        self.assertEqual(self._topics(MessageTopic.ROUTE_REQUEST), [peer.address])
        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [])

    def test_validator_list_that_arrives_during_the_wait_is_used(self) -> None:
        node = _node(validator=False)
        _add(node, _peer(_key(0x30)))
        late = _peer(_key(0x10))
        node.peers[late.public_key_bytes] = late

        def arrive():
            node.validation_route.add_peer(late.public_key_bytes)

        timer = threading.Timer(0.05, arrive)
        timer.start()
        self.addCleanup(timer.cancel)
        send_transaction(node, _signed())
        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [late.address])

    def test_validator_node_queues_on_its_own_node(self) -> None:
        node = _node(validator=True)
        tx = _signed()
        tx_hash = send_transaction(node, tx)
        self.assertIs(node._validation_transaction_queue.get_nowait(), tx)
        self.assertEqual(tx.hash, tx_hash)
        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [])

    def test_validator_node_also_sends_to_other_validators(self) -> None:
        node = _node(validator=True)
        other = _validator_peer(node, _key(0x10))
        send_transaction(node, _signed())
        self.assertFalse(node._validation_transaction_queue.empty())
        self.assertEqual(self._topics(MessageTopic.TRANSACTION), [other.address])

    def test_plain_node_does_not_queue_on_its_own_node(self) -> None:
        node = _node(validator=False)
        _validator_peer(node, _key(0x10))
        send_transaction(node, _signed())
        self.assertTrue(node._validation_transaction_queue.empty())

    def test_not_connected_raises(self) -> None:
        node = _node(validator=False)
        node.is_connected = False
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            send_transaction(node, _signed())


if __name__ == "__main__":
    unittest.main()
