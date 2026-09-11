"""Tests for the storage resolution grid: _collect_missing_hashes, get_expr_from_network, and wire codec."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import (
    Expr,
    ZERO32,
    RESOLUTION_SINGLE,
    RESOLUTION_LIST,
    RESOLUTION_FULL,
    RESOLUTION_RECORD,
    int_,
    symbol,
    bytes_,
    link,
    NIL,
    collect_list,
    collect_full,
)
from astreum.communication.storage_request.handle import _collect_record_exprs
from astreum.communication.storage_response.storage_found import (
    STORAGE_FOUND_PAYLOAD,
    encode_payload,
    decode_payload,
)
from astreum.storage.exprs.network import (
    _collect_missing_hashes,
    _send_storage_request,
    get_expr_from_network,
)
from astreum.storage.exprs import get_expr_from_local_storage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_link(head: Expr | None = None, tail: Expr | None = None) -> Expr:
    """Create a resolved link expr with no hash refs."""
    return link(head, tail)


def _make_hash_ref_link(head_hash: bytes, tail_hash: bytes) -> Expr:
    """Create a link expr with unresolved head_hash / tail_hash (no _head/_tail)."""
    return Expr("link", head_hash=head_hash, tail_hash=tail_hash)


def _make_3_node_chain() -> tuple[Expr, Expr, Expr, Expr]:
    """Build a fully-resolved 3-node chain: root -> node_b -> node_c -> NIL.

    Returns (root, node_b, node_c, node_c_tail_hash).
    """
    node_c = _make_link(int_(1), NIL)
    node_b = _make_link(int_(2), node_c)
    root = _make_link(int_(3), node_b)
    return root, node_b, node_c, node_c.hash()


def _fake_node(
    *,
    is_connected: bool = True,
    hot_storage: dict | None = None,
    fetch_interval: float = 0.01,
    fetch_retries: int = 3,
) -> MagicMock:
    """Create a minimal mock Node for get_expr_from_network tests."""
    node = MagicMock()
    node.is_connected = is_connected
    node.hot_storage = hot_storage or {}
    node.hot_storage_lock = threading.Lock()
    node.config = {
        "storage_fetch_interval": fetch_interval,
        "storage_fetch_retries": fetch_retries,
        "hot_storage_limit": 10 * 1024 * 1024,
        "cold_storage_path": None,
    }
    node.storage_index = {}
    node.storage_providers = []
    node.expr_requests = {}
    node.expr_requests_lock = threading.Lock()
    node.relay_secret_key = MagicMock()
    node.storage_public_key_bytes = b"\x00" * 32
    node.peer_route = MagicMock()
    node.outgoing_queue = MagicMock()
    node.logger = MagicMock()
    return node


# ===========================================================================
# TestCollectMissingHashes
# ===========================================================================

class TestCollectMissingHashes(unittest.TestCase):
    """Unit tests for _collect_missing_hashes — pure logic, no network."""

    def test_single_returns_empty(self) -> None:
        expr = _make_link(int_(1), int_(2))
        self.assertEqual(_collect_missing_hashes(expr, RESOLUTION_SINGLE), [])

    def test_single_returns_empty_for_non_link(self) -> None:
        expr = int_(42)
        self.assertEqual(_collect_missing_hashes(expr, RESOLUTION_SINGLE), [])

    def test_list_with_unresolved_tail(self) -> None:
        tail_hash = b"\xab" * 32
        root = _make_hash_ref_link(ZERO32, tail_hash)
        missing = _collect_missing_hashes(root, RESOLUTION_LIST)
        self.assertEqual(missing, [tail_hash])

    def test_list_fully_resolved(self) -> None:
        root = _make_link(NIL, _make_link(int_(1), NIL))
        self.assertEqual(_collect_missing_hashes(root, RESOLUTION_LIST), [])

    def test_list_stops_at_first_missing(self) -> None:
        """3-node chain where middle node has unresolved tail — only the first missing hash is returned."""
        leaf = _make_link(int_(1), NIL)
        middle = _make_link(int_(2), leaf)
        root = _make_link(int_(3), middle)

        # Mutate leaf to have unresolved tail hash (simulate partial fetch)
        leaf._tail = None
        leaf._tail_hash = b"\xcd" * 32

        missing = _collect_missing_hashes(root, RESOLUTION_LIST)
        # Should find leaf's unresolved tail hash
        self.assertEqual(missing, [b"\xcd" * 32])

    def test_list_non_link_returns_empty(self) -> None:
        expr = int_(99)
        self.assertEqual(_collect_missing_hashes(expr, RESOLUTION_LIST), [])

    def test_full_with_unresolved_head_and_tail(self) -> None:
        head_hash = b"\x11" * 32
        tail_hash = b"\x22" * 32
        root = _make_hash_ref_link(head_hash, tail_hash)
        missing = _collect_missing_hashes(root, RESOLUTION_FULL)
        self.assertEqual(sorted(missing), sorted([head_hash, tail_hash]))

    def test_full_with_only_unresolved_head(self) -> None:
        tail_node = _make_link(int_(1), NIL)
        head_hash = b"\x33" * 32
        root = Expr("link", head_hash=head_hash, tail=tail_node)
        missing = _collect_missing_hashes(root, RESOLUTION_FULL)
        self.assertEqual(missing, [head_hash])

    def test_full_with_only_unresolved_tail(self) -> None:
        head_node = _make_link(int_(1), NIL)
        tail_hash = b"\x44" * 32
        root = Expr("link", head=head_node, tail_hash=tail_hash)
        missing = _collect_missing_hashes(root, RESOLUTION_FULL)
        self.assertEqual(missing, [tail_hash])

    def test_full_dfs_walk(self) -> None:
        """DFS walks head before tail. Deeply nested with some unresolved."""
        deep_leaf_hash = b"\x55" * 32
        deep_leaf = _make_hash_ref_link(deep_leaf_hash, ZERO32)

        shallow_tail = _make_link(int_(7), NIL)
        root = _make_link(deep_leaf, shallow_tail)

        missing = _collect_missing_hashes(root, RESOLUTION_FULL)
        self.assertIn(deep_leaf_hash, missing)

    def test_full_fully_resolved(self) -> None:
        tree = _make_link(_make_link(int_(1), int_(2)), _make_link(int_(3), int_(4)))
        self.assertEqual(_collect_missing_hashes(tree, RESOLUTION_FULL), [])


# ===========================================================================
# TestObjectFoundCodec
# ===========================================================================

class TestObjectFoundCodec(unittest.TestCase):
    """Tests for encode_payload / decode_payload roundtrip."""

    def test_single_roundtrip(self) -> None:
        expr = int_(42)
        encoded = encode_payload([expr])
        decoded = decode_payload(encoded[1:])  # strip type byte
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].hash(), expr.hash())

    def test_multi_roundtrip(self) -> None:
        exprs = [int_(i) for i in range(5)]
        encoded = encode_payload(exprs)
        decoded = decode_payload(encoded[1:])
        self.assertEqual(len(decoded), 5)
        for original, got in zip(exprs, decoded):
            self.assertEqual(original.hash(), got.hash())

    def test_type_byte_is_1(self) -> None:
        encoded = encode_payload([int_(1)])
        self.assertEqual(encoded[0], STORAGE_FOUND_PAYLOAD)
        self.assertEqual(encoded[0], 1)

    def test_link_roundtrip(self) -> None:
        head = int_(10)
        tail = int_(20)
        root = link(head, tail)
        encoded = encode_payload([root])
        decoded = decode_payload(encoded[1:])
        self.assertEqual(decoded[0].hash(), root.hash())

    def test_decode_empty_returns_empty_list(self) -> None:
        result = decode_payload(b"")
        self.assertEqual(result, [])

    def test_decode_truncated_length_raises(self) -> None:
        with self.assertRaises(ValueError):
            decode_payload(b"\x00\x01")  # 2 bytes < 4-byte length prefix

    def test_decode_truncated_payload_raises(self) -> None:
        # Valid length prefix claiming 100 bytes, but only 2 bytes follow
        payload = (100).to_bytes(4, "big") + b"\x00\x01"
        with self.assertRaises(ValueError):
            decode_payload(payload)

    def test_decode_invalid_length_zero_raises(self) -> None:
        payload = (0).to_bytes(4, "big")
        with self.assertRaises(ValueError):
            decode_payload(payload)


# ===========================================================================
# TestCollectRecordExprs
# ===========================================================================

class TestCollectRecordExprs(unittest.TestCase):
    """Unit tests for the record-aware serving assembly."""

    def test_no_records_entry_returns_none(self) -> None:
        node = _fake_node()
        root = int_(1)
        with patch(
            "astreum.communication.storage_request.handle.get_record_from_cold_storage",
            return_value=None,
        ):
            self.assertIsNone(_collect_record_exprs(node, root, root.hash()))

    def test_root_first_and_slots_in_blob_order(self) -> None:
        node = _fake_node()
        root = int_(1)
        slot_a = int_(2)
        slot_b = int_(3)
        blob = slot_a.hash() + ZERO32 + slot_b.hash()

        def fake_local(_node, expr_id):
            return {slot_a.hash(): slot_a, slot_b.hash(): slot_b}.get(expr_id)

        with patch(
            "astreum.communication.storage_request.handle.get_record_from_cold_storage",
            return_value=blob,
        ), patch(
            "astreum.communication.storage_request.handle.get_expr_from_local_storage",
            side_effect=fake_local,
        ):
            exprs = _collect_record_exprs(node, root, root.hash())

        self.assertEqual(
            [e.hash() for e in exprs],
            [root.hash(), slot_a.hash(), slot_b.hash()],
        )

    def test_missing_and_zero_slots_are_skipped(self) -> None:
        node = _fake_node()
        root = int_(1)
        present = int_(2)
        missing = int_(3)
        blob = present.hash() + ZERO32 + missing.hash()

        with patch(
            "astreum.communication.storage_request.handle.get_record_from_cold_storage",
            return_value=blob,
        ), patch(
            "astreum.communication.storage_request.handle.get_expr_from_local_storage",
            side_effect=lambda _node, h: present if h == present.hash() else None,
        ):
            exprs = _collect_record_exprs(node, root, root.hash())

        self.assertEqual([e.hash() for e in exprs], [root.hash(), present.hash()])


# ===========================================================================
# TestGetExprFromNetwork
# ===========================================================================

class TestGetExprFromNetwork(unittest.TestCase):
    """Tests for get_expr_from_network — mocked network, polling, retry logic."""

    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_returns_none_when_disconnected(self, mock_send: MagicMock) -> None:
        node = _fake_node(is_connected=False)
        result = get_expr_from_network(node, b"\x00" * 32, RESOLUTION_SINGLE)
        self.assertIsNone(result)
        mock_send.assert_not_called()

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_single_poll_success(self, mock_send: MagicMock, mock_sleep: MagicMock) -> None:
        node = _fake_node()
        target = int_(99)
        target_hash = target.hash()

        # First call returns None, then the expr appears in hot storage
        call_count = 0

        def fake_get(n, h):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return target
            return None

        with patch("astreum.storage.exprs.network.get_expr_from_local_storage", side_effect=fake_get):
            result = get_expr_from_network(node, target_hash, RESOLUTION_SINGLE)

        self.assertIsNotNone(result)
        self.assertEqual(result.hash(), target_hash)

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_single_poll_timeout(self, mock_send: MagicMock, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=3)

        with patch("astreum.storage.exprs.network.get_expr_from_local_storage", return_value=None):
            result = get_expr_from_network(node, b"\xaa" * 32, RESOLUTION_SINGLE)

        self.assertIsNone(result)

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_send_request_error_returns_none(self, mock_send: MagicMock, mock_sleep: MagicMock) -> None:
        node = _fake_node()
        mock_send.return_value = "no peer available"

        result = get_expr_from_network(node, b"\xbb" * 32, RESOLUTION_SINGLE)
        self.assertIsNone(result)
        mock_send.assert_called_once()

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network.get_expr_from_network")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_list_poll_with_missing_tail(
        self,
        mock_send: MagicMock,
        mock_network: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """When list has unresolved tail hashes, recursive fetches are triggered."""
        node = _fake_node(fetch_retries=1)

        tail_hash = b"\xcc" * 32
        root = _make_hash_ref_link(ZERO32, tail_hash)

        # get_expr_list_from_local_storage returns the partially-resolved root
        with patch("astreum.storage.exprs.list.get_expr_list_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_LIST)

        # Recursive fetch should have been called for the missing tail
        mock_network.assert_called_with(node, tail_hash, RESOLUTION_SINGLE)

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network.get_expr_from_network")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_full_poll_with_missing_inner(
        self,
        mock_send: MagicMock,
        mock_network: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """When full expr has unresolved inner hashes, recursive fetches are triggered."""
        node = _fake_node(fetch_retries=1)

        head_hash = b"\xdd" * 32
        tail_hash = b"\xee" * 32
        root = _make_hash_ref_link(head_hash, tail_hash)

        with patch("astreum.storage.exprs.full.get_expr_full_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_FULL)

        # Both head and tail should be recursively fetched
        calls = [c.args for c in mock_network.call_args_list]
        self.assertIn((node, head_hash, RESOLUTION_SINGLE), calls)
        self.assertIn((node, tail_hash, RESOLUTION_SINGLE), calls)

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_list_poll_no_missing(self, mock_send: MagicMock, mock_sleep: MagicMock) -> None:
        """Fully-resolved list returns immediately without recursive fetch."""
        node = _fake_node(fetch_retries=3)
        root = _make_link(int_(1), _make_link(int_(2), NIL))

        with patch("astreum.storage.exprs.list.get_expr_list_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_LIST)

        self.assertIsNotNone(result)
        self.assertEqual(result.hash(), root.hash())

    @patch("astreum.storage.exprs.network.sleep")
    @patch("astreum.storage.exprs.network._send_storage_request", return_value=None)
    def test_full_poll_no_missing(self, mock_send: MagicMock, mock_sleep: MagicMock) -> None:
        """Fully-resolved full expr returns immediately."""
        node = _fake_node(fetch_retries=3)
        root = _make_link(_make_link(int_(1), int_(2)), _make_link(int_(3), int_(4)))

        with patch("astreum.storage.exprs.full.get_expr_full_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_FULL)

        self.assertIsNotNone(result)
        self.assertEqual(result.hash(), root.hash())


# ===========================================================================
# TestSendStorageRequest
# ===========================================================================

class TestSendStorageRequest(unittest.TestCase):
    """Tests for _send_storage_request — indexed provider vs DHT fallback."""

    def test_indexed_provider_path(self) -> None:
        """When storage_index has a hit, request goes to the indexed provider."""
        node = _fake_node()

        # Mock the provider payload decode + key exchange
        fake_provider_payload = b"\x00" * 70  # storage_key(32) + relay_key(32) + ip(4) + port(2)
        node.storage_providers.append(fake_provider_payload)
        node.storage_index[b"\xaa" * 32] = 0  # provider_id = 0

        with patch("astreum.storage.providers.provider_payload_for_id", return_value=fake_provider_payload), \
             patch("astreum.communication.storage_response.storage_provider.decode_storage_provider") as mock_decode, \
             patch("astreum.communication.outgoing_queue.enqueue_outgoing", return_value=True) as mock_enqueue, \
             patch("cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey") as mock_x25519:

            mock_decode.return_value = (b"\x00" * 32, b"\x00" * 32, "127.0.0.1", 5000)
            mock_x25519.from_public_bytes.return_value = MagicMock()
            node.relay_secret_key.exchange.return_value = b"\x01" * 32

            result = _send_storage_request(node, b"\xaa" * 32, RESOLUTION_SINGLE)

        self.assertIsNone(result)
        self.assertIn(b"\xaa" * 32, node.expr_requests)

    def test_dht_fallback_path(self) -> None:
        """When no index hit, request goes to closest peer."""
        node = _fake_node()

        mock_peer = MagicMock()
        mock_peer.address = ("127.0.0.1", 5001)
        mock_peer.shared_key_bytes = b"\x02" * 32
        mock_peer.difficulty = 1
        node.peer_route.closest_peer_for_hash.return_value = mock_peer

        with patch("astreum.communication.outgoing_queue.enqueue_outgoing", return_value=True) as mock_enqueue:
            result = _send_storage_request(node, b"\xbb" * 32, RESOLUTION_LIST)

        self.assertIsNone(result)
        self.assertIn(b"\xbb" * 32, node.expr_requests)
        mock_enqueue.assert_called_once()

    def test_no_peer_available(self) -> None:
        """When closest peer is None, returns error."""
        node = _fake_node()
        node.peer_route.closest_peer_for_hash.return_value = None

        result = _send_storage_request(node, b"\xcc" * 32, RESOLUTION_SINGLE)
        self.assertEqual(result, "no peer available")

    def test_unknown_provider_id(self) -> None:
        """When index has unknown provider id, returns error."""
        node = _fake_node()
        node.storage_index[b"\xdd" * 32] = 999  # non-existent provider

        result = _send_storage_request(node, b"\xdd" * 32, RESOLUTION_SINGLE)
        self.assertIn("unknown provider id", result)


if __name__ == "__main__":
    unittest.main()
