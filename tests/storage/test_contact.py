"""A ``contact`` passed to the read functions reaches the GET that is sent.

V is an in-memory store. The requester has an empty store; the patched
``queue_storage_get`` answers from V and records the contact it was given.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.communication.models.relay_endpoint import RelayEndpoint  # noqa: E402
from astreum.consensus.block.create import create_block  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.expression.encoding import (  # noqa: E402
    decode_expr_from_bytes,
    encode_expr_to_bytes,
)
from astreum.consensus.block.message import block_header_exprs  # noqa: E402
from astreum.expression.expr import TAG_BYTE_ENCODINGS  # noqa: E402
from astreum.expression import (  # noqa: E402
    NIL,
    ZERO32,
    bytes_,
    link,
    resolve_inner_exprs,
)
from astreum.storage.exprs import (  # noqa: E402
    get_expr,
    get_expr_full,
    get_expr_list,
    get_exprs_from_network,
    put_expr_in_hot_storage,
)
from astreum.storage.radix import (  # noqa: E402
    RadixTree,
    get_all_from_radix_tree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.utils.config import NodeConfig

CONTACT = RelayEndpoint(b"\x07" * 32, ("10.0.0.7", 7000))
QUEUE = "astreum.storage.workers.requests.queue_storage_get"


def _put(node, expr) -> None:
    """Store *expr* as cold storage does: with the bytes child of each composite."""
    put_expr_in_hot_storage(node, expr)
    stack = [expr]
    while stack:
        e = stack.pop()
        if e is None or e.base != "link":
            continue
        if e.head is None and e.value is not None and e.tail is not None and e.tail.base == "symbol":
            encoder = TAG_BYTE_ENCODINGS.get(e.tail.value)
            if encoder is not None:
                put_expr_in_hot_storage(node, bytes_(encoder(e.value)))
        stack.extend([e.head, e.tail])


def _store_node() -> SimpleNamespace:
    return SimpleNamespace(
        hot_storage={},
        hot_storage_timestamps={},
        hot_storage_lock=threading.RLock(),
        hot_storage_size=0,
        config=NodeConfig(
            hot_storage_limit=64 * 1024 * 1024,
            cold_storage_path=None,
            storage_fetch_interval=0,
            storage_fetch_retries=1,
        ),
        is_connected=True,
        logger=MagicMock(),
    )


class _Served:
    """Patch ``queue_storage_get`` so V (``source``) answers every GET."""

    def __init__(self, source: SimpleNamespace, requester: SimpleNamespace):
        self.source = source
        self.requester = requester
        self.calls: list[tuple[bytes, object]] = []

    def __enter__(self):
        def fake_queue(node, expr_id, resolution, *, contact=None, preclaimed=False):
            self.calls.append((expr_id, contact))
            expr = self.source.hot_storage.get(expr_id)
            if expr is None:
                return False
            # Served flat (hash references only), as it arrives over the wire.
            self.requester.hot_storage[expr_id] = decode_expr_from_bytes(
                encode_expr_to_bytes(expr)
            )
            return True

        self._patch = patch(QUEUE, side_effect=fake_queue)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    @property
    def contacts(self) -> set:
        return {c for _, c in self.calls}


class TestExprReads(unittest.TestCase):
    def setUp(self) -> None:
        self.v = _store_node()
        self.b = _store_node()

    def test_get_expr_sends_to_the_contact(self) -> None:
        expr = bytes_(b"only on V")
        put_expr_in_hot_storage(self.v, expr)
        with _Served(self.v, self.b) as served:
            result = get_expr(self.b, expr.hash(), contact=CONTACT)
        self.assertEqual(result.hash(), expr.hash())
        self.assertEqual(served.calls, [(expr.hash(), CONTACT)])

    def test_without_contact_routing_is_unchanged(self) -> None:
        expr = bytes_(b"anywhere")
        put_expr_in_hot_storage(self.v, expr)
        with _Served(self.v, self.b) as served:
            get_expr(self.b, expr.hash())
        self.assertEqual(served.contacts, {None})

    def test_local_hit_skips_the_network(self) -> None:
        expr = bytes_(b"already here")
        put_expr_in_hot_storage(self.b, expr)
        with _Served(self.v, self.b) as served:
            self.assertEqual(get_expr(self.b, expr.hash(), contact=CONTACT).hash(), expr.hash())
        self.assertEqual(served.calls, [])

    def test_batch_fetch_goes_to_the_contact(self) -> None:
        exprs = [bytes_(b"a"), bytes_(b"b")]
        for e in exprs:
            put_expr_in_hot_storage(self.v, e)
        with _Served(self.v, self.b) as served:
            get_exprs_from_network(
                self.b, [(e.hash(), 0) for e in exprs], contact=CONTACT
            )
        self.assertEqual(served.contacts, {CONTACT})
        self.assertEqual(len(served.calls), 2)

    def test_list_tail_chain_stays_on_the_contact(self) -> None:
        items = [bytes_(bytes([i])) for i in range(3)]
        chain = NIL
        for item in reversed(items):
            chain = link(item, chain)
        for e in resolve_inner_exprs(self.v, chain)[0]:
            put_expr_in_hot_storage(self.v, e)
        with _Served(self.v, self.b) as served:
            got = get_expr_list(self.b, chain.hash(), contact=CONTACT)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(len(served.calls), 2)
        self.assertEqual(served.contacts, {CONTACT})

    def test_full_tree_stays_on_the_contact(self) -> None:
        tree = link(link(bytes_(b"x"), bytes_(b"y")), link(bytes_(b"z"), NIL))
        for e in resolve_inner_exprs(self.v, tree)[0]:
            put_expr_in_hot_storage(self.v, e)
        with _Served(self.v, self.b) as served:
            got = get_expr_full(self.b, tree.hash(), contact=CONTACT)
        self.assertIsNotNone(got)
        self.assertTrue(served.calls)
        self.assertEqual(served.contacts, {CONTACT})

    def test_miss_on_the_contact_returns_none(self) -> None:
        with _Served(self.v, self.b) as served:
            self.assertIsNone(get_expr(self.b, b"\x09" * 32, contact=CONTACT))
        self.assertEqual(served.contacts, {CONTACT})


class TestRadixTreeContact(unittest.TestCase):
    def setUp(self) -> None:
        self.v = _store_node()
        self.b = _store_node()
        built = RadixTree()
        self.values = {bytes([i]) * 4: bytes_(bytes([i]) * 8) for i in range(1, 6)}
        for key, value in self.values.items():
            put_in_radix_tree(built, self.v, key, value)
        for node in built.nodes.values():
            _put(self.v, get_radix_node_expr(node))
            if node.value is not None and not isinstance(node.value, bytes):
                _put(self.v, node.value)
        self.root = built.root_hash

    def test_get_fetches_nodes_from_the_contact(self) -> None:
        tree = RadixTree(root_hash=self.root, contact=CONTACT)
        key = bytes([3]) * 4
        with _Served(self.v, self.b) as served:
            got = get_from_radix_tree(tree, self.b, key)
        self.assertEqual(got.hash(), self.values[key].hash())
        self.assertTrue(served.calls)
        self.assertEqual(served.contacts, {CONTACT})

    def test_get_all_fetches_nodes_from_the_contact(self) -> None:
        tree = RadixTree(root_hash=self.root, contact=CONTACT)
        with _Served(self.v, self.b) as served:
            everything = get_all_from_radix_tree(tree, self.b)
        self.assertEqual(set(everything), set(self.values))
        self.assertEqual(served.contacts, {CONTACT})

    def test_tree_without_contact_routes_normally(self) -> None:
        tree = RadixTree(root_hash=self.root)
        with _Served(self.v, self.b) as served:
            get_from_radix_tree(tree, self.b, bytes([3]) * 4)
        self.assertEqual(served.contacts, {None})

    def test_clone_keeps_the_contact(self) -> None:
        from astreum.storage.radix import clone_radix_tree

        self.assertEqual(clone_radix_tree(RadixTree(contact=CONTACT)).contact, CONTACT)


def _block(**overrides):
    kwargs = dict(
        chain_id=0,
        previous_block_hash=b"\x01" * 32,
        previous_block=None,
        height=1,
        timestamp=100,
        accounts_hash=b"\x0a" * 32,
        total_transaction_fee=0,
        total_storage_fee=0,
        statistics=[(1, 1, 0, 0)],
        transactions_hash=ZERO32,
        receipts_hash=ZERO32,
        difficulty=1,
        validator_public_key_bytes=b"\x02" * 32,
        signature=b"\x03" * 64,
    )
    kwargs.update(overrides)
    return create_block(**kwargs)


class TestBlockContact(unittest.TestCase):
    def test_block_hands_its_contact_to_the_accounts_trie(self) -> None:
        block = _block(contact=CONTACT)
        self.assertEqual(block.contact, CONTACT)
        self.assertEqual(block.accounts.contact, CONTACT)
        self.assertEqual(block.accounts._trie.contact, CONTACT)

    def test_accounts_hand_the_contact_to_the_accounts_they_load(self) -> None:
        self.assertEqual(Accounts(root_hash=b"\x01" * 32, contact=CONTACT)._trie.contact, CONTACT)

    def test_block_without_contact(self) -> None:
        block = _block()
        self.assertIsNone(block.contact)
        self.assertIsNone(block.accounts._trie.contact)

    def test_loading_a_block_fetches_its_header_from_the_contact(self) -> None:
        v, b = _store_node(), _store_node()
        block = _block()
        block_hash = get_block_expr(block).hash()
        for e in block_header_exprs(get_block_expr(block)):
            put_expr_in_hot_storage(v, e)
        with _Served(v, b) as served:
            loaded = get_block_from_storage(b, block_hash, contact=CONTACT, load_bloom=False)
        self.assertEqual(loaded.height, 1)
        self.assertTrue(served.calls)
        self.assertEqual(served.contacts, {CONTACT})
        self.assertEqual(loaded.contact, CONTACT)
        self.assertEqual(loaded.accounts._trie.contact, CONTACT)


class TestDestination(unittest.TestCase):
    def test_destination_uses_endpoint_fields(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        from astreum.storage.workers.requests import _resolve_destination

        provider = X25519PrivateKey.generate()
        relay = provider.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        node = SimpleNamespace(
            relay_secret_key=X25519PrivateKey.generate(), logger=MagicMock()
        )
        endpoint = RelayEndpoint(relay, ("10.1.1.1", 6000))
        key, address, shared, difficulty = _resolve_destination(
            node, b"\x01" * 32, contact=endpoint
        )
        self.assertEqual(key, ("provider", relay, "10.1.1.1", 6000))
        self.assertEqual(address, ("10.1.1.1", 6000))
        self.assertEqual(shared, node.relay_secret_key.exchange(provider.public_key()))
        self.assertEqual(difficulty, 1)

    def test_tuple_contact_is_not_accepted(self) -> None:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        from astreum.storage.workers.requests import _resolve_destination

        node = SimpleNamespace(
            relay_secret_key=X25519PrivateKey.generate(), logger=MagicMock()
        )
        self.assertIsNone(
            _resolve_destination(node, b"\x01" * 32, contact=(b"\x07" * 32, "10.0.0.7", 7000))
        )


if __name__ == "__main__":
    unittest.main()
