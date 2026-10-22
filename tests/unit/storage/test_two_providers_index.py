"""Tests for index_provider."""
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.util import xor_distance
from astreum.storage.providers import index_provider, provider_id_for_payload

EXPR = b"\x00" * 32


def _payload(key_byte: int, port: int = 5000) -> bytes:
    return (
        bytes([key_byte]) * 32
        + b"\x77" * 32
        + socket.inet_aton("10.0.0.1")
        + port.to_bytes(2, "big")
    )


def _node():
    return SimpleNamespace(storage_index={}, storage_providers=[])


def _add(node, payload, expr_id=EXPR):
    provider_id = provider_id_for_payload(node, payload)
    index_provider(node, expr_id, provider_id)
    return provider_id


class TestIndexProvider(unittest.TestCase):
    def test_first_provider_stored(self):
        node = _node()
        a = _add(node, _payload(5))
        self.assertEqual(node.storage_index[EXPR], (a,))

    def test_second_provider_added(self):
        node = _node()
        a = _add(node, _payload(5))
        b = _add(node, _payload(9))
        self.assertEqual(node.storage_index[EXPR], (a, b))

    def test_list_sorted_nearest_first(self):
        for order in ((9, 5), (5, 9)):
            node = _node()
            ids = {k: _add(node, _payload(k)) for k in order}
            self.assertEqual(node.storage_index[EXPR], (ids[5], ids[9]))

    def test_same_provider_not_duplicated(self):
        node = _node()
        a = _add(node, _payload(5))
        _add(node, _payload(5))
        self.assertEqual(node.storage_index[EXPR], (a,))

    def test_same_key_new_payload_replaces(self):
        node = _node()
        _add(node, _payload(5, 5000))
        new = _add(node, _payload(5, 6000))
        self.assertEqual(node.storage_index[EXPR], (new,))

    def test_third_provider_keeps_two_nearest(self):
        node = _node()
        ids = {k: _add(node, _payload(k)) for k in (9, 5, 7)}
        self.assertEqual(node.storage_index[EXPR], (ids[5], ids[7]))

    def test_bad_payload_dropped(self):
        node = _node()
        provider_id = provider_id_for_payload(node, b"\x01" * 10)
        index_provider(node, EXPR, provider_id)
        self.assertEqual(node.storage_index, {})

    def test_unknown_provider_id_dropped(self):
        node = _node()
        index_provider(node, EXPR, 42)
        self.assertEqual(node.storage_index, {})

    def test_update_replaces_tuple(self):
        node = _node()
        _add(node, _payload(5))
        before = node.storage_index[EXPR]
        _add(node, _payload(9))
        self.assertIsInstance(before, tuple)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(node.storage_index[EXPR]), 2)


if __name__ == "__main__":
    unittest.main()
