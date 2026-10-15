"""Tests for the announce after a record is stored."""
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.providers import index_provider, provider_id_for_payload
from astreum.storage.records import get_record_from_cold_storage
from astreum.storage.records.fetch import _announce_record
from tests.storage import test_long_term as lt

PUT = "astreum.storage.exprs.network.put_exprs_in_network"
EXPR = b"\x00" * 32
OWN_KEY = b"\x02" * 32


def _payload(key_byte: int) -> bytes:
    return (
        bytes([key_byte]) * 32
        + b"\x77" * 32
        + socket.inet_aton("10.0.0.1")
        + (5000).to_bytes(2, "big")
    )


def _logger():
    return SimpleNamespace(debug=lambda *a, **k: None)


def _node(nearer_peer=None):
    """A node that is the provider with storage key OWN_KEY."""
    return SimpleNamespace(
        storage_index={},
        storage_providers=[],
        relay_ip_address="10.0.0.9",
        config={
            "port": 7000,
            "storage_public_key_bytes": OWN_KEY,
            "relay_public_key_bytes": b"\x88" * 32,
        },
        peer_route=SimpleNamespace(closest_peer_for_hash=lambda h: nearer_peer),
        logger=_logger(),
    )


class TestAnnounceAfterStore(unittest.TestCase):
    setUp = lt.TestFetchAndStoreRecord.setUp
    tearDown = lt.TestFetchAndStoreRecord.tearDown
    _creator_record = lt.TestFetchAndStoreRecord._creator_record
    _run = lt.TestFetchAndStoreRecord._run
    GET_EXPR = lt.TestFetchAndStoreRecord.GET_EXPR
    GET_BLOCK = lt.TestFetchAndStoreRecord.GET_BLOCK
    ROOT_GET = lt.TestFetchAndStoreRecord.ROOT_GET
    TRIE = lt.TestFetchAndStoreRecord.TRIE

    def test_store_announces_record(self):
        with patch(PUT) as put:
            self.assertTrue(self._run())
        put.assert_called_once_with(self.node, [(self.storage_id, RESOLUTION_SINGLE)])

    def test_failed_store_does_not_announce(self):
        bad = lt.StorageRecord(
            **{**self.record.__dict__, "slots_root": b"\x77" * 32, "_expr": None}
        )
        with patch(PUT) as put:
            self.assertFalse(self._run(record=bad))
        put.assert_not_called()

    def test_announce_after_store(self):
        seen = []

        def announce(node, entries):
            seen.append(get_record_from_cold_storage(node, entries[0][0]))

        with patch(PUT, side_effect=announce):
            self.assertTrue(self._run())
        self.assertEqual(seen, [b"".join(self.slots)])

    def test_announce_error_keeps_store(self):
        with patch(PUT, side_effect=RuntimeError("boom")):
            self.assertTrue(self._run())
        self.assertEqual(get_record_from_cold_storage(self.node, self.storage_id), b"".join(self.slots))


class TestAnnounceIndex(unittest.TestCase):
    def _own_id(self, node):
        return provider_id_for_payload(node, _node_payload(node))

    def test_nearest_node_names_itself(self):
        node = _node()
        _announce_record(node, EXPR)
        self.assertEqual(node.storage_index[EXPR], (self._own_id(node),))

    def test_nearer_node_displaces_farther_provider(self):
        node = _node()
        near = provider_id_for_payload(node, _payload(1))
        far = provider_id_for_payload(node, _payload(9))
        index_provider(node, EXPR, far)
        index_provider(node, EXPR, near)
        # OWN_KEY (0x02) is nearer to EXPR (0x00) than 0x09 and farther than 0x01.
        _announce_record(node, EXPR)
        self.assertEqual(node.storage_index[EXPR], (near, self._own_id(node)))

    def test_put_then_store_names_itself(self):
        node = _node()
        far = provider_id_for_payload(node, _payload(9))
        index_provider(node, EXPR, far)
        _announce_record(node, EXPR)
        self.assertEqual(node.storage_index[EXPR], (self._own_id(node), far))

    def test_nearer_peer_gets_put(self):
        peer = SimpleNamespace(public_key_bytes=b"\x01" * 32, address=("10.0.0.5", 7000))
        node = _node(nearer_peer=peer)
        with patch("astreum.storage.exprs.network.send_put_entries", return_value=[]) as send:
            _announce_record(node, EXPR)
        self.assertEqual(node.storage_index, {})
        send.assert_called_once()
        self.assertIs(send.call_args.args[1], peer)
        self.assertEqual(send.call_args.args[3], [(EXPR, RESOLUTION_SINGLE)])


def _node_payload(node) -> bytes:
    from astreum.storage.exprs.network import build_provider_payload

    return build_provider_payload(node)


if __name__ == "__main__":
    unittest.main()
