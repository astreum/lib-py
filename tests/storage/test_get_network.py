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
    encode_found_pages,
    decode_found_page,
)
import astreum.storage.exprs.network as net
from astreum.storage.exprs.network import (
    _collect_missing_hashes,
    get_expr_from_network,
    get_exprs_from_network,
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
    """Tests for encode_found_pages / decode_found_page roundtrip."""

    def test_single_roundtrip(self) -> None:
        expr = int_(42)
        pages = encode_found_pages([expr])
        self.assertEqual(len(pages), 1)
        page, total, decoded = decode_found_page(pages[0])
        self.assertEqual((page, total), (1, 1))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].hash(), expr.hash())

    def test_multi_roundtrip(self) -> None:
        exprs = [int_(i) for i in range(5)]
        _, _, decoded = decode_found_page(encode_found_pages(exprs)[0])
        self.assertEqual(len(decoded), 5)
        for original, got in zip(exprs, decoded):
            self.assertEqual(original.hash(), got.hash())

    def test_type_byte_is_1(self) -> None:
        encoded = encode_found_pages([int_(1)])[0]
        self.assertEqual(encoded[0], STORAGE_FOUND_PAYLOAD)
        self.assertEqual(encoded[0], 1)

    def test_link_roundtrip(self) -> None:
        root = link(int_(10), int_(20))
        _, _, decoded = decode_found_page(encode_found_pages([root])[0])
        self.assertEqual(decoded[0].hash(), root.hash())

    def test_decode_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            decode_found_page(b"")

    def test_decode_truncated_length_raises(self) -> None:
        header = bytes([1, 0, 1, 0, 1, 0, 1])
        with self.assertRaises(ValueError):
            decode_found_page(header + b"\x00\x01")  # 2 bytes < 4-byte length prefix

    def test_decode_truncated_payload_raises(self) -> None:
        header = bytes([1, 0, 1, 0, 1, 0, 1])
        payload = header + (100).to_bytes(4, "big") + b"\x00\x01"
        with self.assertRaises(ValueError):
            decode_found_page(payload)

    def test_decode_invalid_length_zero_raises(self) -> None:
        header = bytes([1, 0, 1, 0, 1, 0, 1])
        with self.assertRaises(ValueError):
            decode_found_page(header + (0).to_bytes(4, "big"))


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

QUEUE = "astreum.storage.workers.requests.queue_storage_get"
LOCAL = "astreum.storage.exprs.network.get_expr_from_local_storage"


class TestGetExprFromNetwork(unittest.TestCase):
    """get_expr_from_network / get_exprs_from_network: mocked buffer and polling."""

    def test_returns_none_when_disconnected(self) -> None:
        node = _fake_node(is_connected=False)
        with patch(QUEUE) as queue:
            result = get_expr_from_network(node, b"\x00" * 32, RESOLUTION_SINGLE)
        self.assertIsNone(result)
        queue.assert_not_called()

    @patch("astreum.storage.exprs.network.sleep")
    def test_single_poll_success(self, mock_sleep: MagicMock) -> None:
        node = _fake_node()
        target = int_(99)
        calls = []

        def fake_get(n, h):
            calls.append(h)
            # miss on the pre-check and first poll, hit on the second poll
            return target if len(calls) >= 3 else None

        with patch(QUEUE, return_value=True) as queue, patch(LOCAL, side_effect=fake_get):
            result = get_expr_from_network(node, target.hash(), RESOLUTION_SINGLE)

        self.assertEqual(result.hash(), target.hash())
        queue.assert_called_once_with(node, target.hash(), RESOLUTION_SINGLE)

    @patch("astreum.storage.exprs.network.sleep")
    def test_single_poll_timeout(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=3)
        with patch(QUEUE, return_value=True), patch(LOCAL, return_value=None):
            result = get_expr_from_network(node, b"\xaa" * 32, RESOLUTION_SINGLE)
        self.assertIsNone(result)

    @patch("astreum.storage.exprs.network.sleep")
    def test_nothing_to_send_to_returns_none_without_polling(self, mock_sleep: MagicMock) -> None:
        node = _fake_node()
        with patch(QUEUE, return_value=False), patch(LOCAL, return_value=None):
            result = get_expr_from_network(node, b"\xbb" * 32, RESOLUTION_SINGLE)
        self.assertIsNone(result)
        mock_sleep.assert_not_called()

    def test_single_already_local_is_not_requested(self) -> None:
        node = _fake_node()
        target = int_(5)
        with patch(QUEUE, return_value=True) as queue, patch(LOCAL, return_value=target):
            result = get_expr_from_network(node, target.hash(), RESOLUTION_SINGLE)
        self.assertEqual(result.hash(), target.hash())
        queue.assert_not_called()

    @patch("astreum.storage.exprs.network.sleep")
    def test_batch_returns_dict_with_none_for_timeouts(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=2)
        a, b, c = int_(1), int_(2), int_(3)
        stored = {a.hash(): a}

        def fake_queue(n, h, r):
            return h != c.hash()  # c has no destination

        with patch(QUEUE, side_effect=fake_queue) as queue, \
             patch(LOCAL, side_effect=lambda n, h: stored.get(h)):
            result = get_exprs_from_network(
                node,
                [(a.hash(), RESOLUTION_SINGLE), (b.hash(), RESOLUTION_SINGLE), (c.hash(), RESOLUTION_SINGLE)],
            )

        self.assertEqual(set(result), {a.hash(), b.hash(), c.hash()})
        self.assertEqual(result[a.hash()].hash(), a.hash())
        self.assertIsNone(result[b.hash()])
        self.assertIsNone(result[c.hash()])
        # a was local, so only b and c were requested
        self.assertEqual([c_.args[1] for c_ in queue.call_args_list], [b.hash(), c.hash()])

    @patch("astreum.storage.exprs.network.sleep")
    def test_batch_waits_overlap(self, mock_sleep: MagicMock) -> None:
        """Hashes that never arrive share one poll window, not one each."""
        node = _fake_node(fetch_retries=3)
        hashes = [bytes([i]) * 32 for i in range(1, 6)]
        with patch(QUEUE, return_value=True), patch(LOCAL, return_value=None):
            get_exprs_from_network(node, [(h, RESOLUTION_SINGLE) for h in hashes])
        self.assertEqual(mock_sleep.call_count, 3)

    @patch("astreum.storage.exprs.network.sleep")
    def test_duplicate_hash_requested_once(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=1)
        h = b"\x01" * 32
        with patch(QUEUE, return_value=True) as queue, patch(LOCAL, return_value=None):
            get_exprs_from_network(node, [(h, RESOLUTION_SINGLE), (h, RESOLUTION_SINGLE)])
        queue.assert_called_once()

    @patch("astreum.storage.exprs.network.sleep")
    def test_list_holes_submitted_together(self, mock_sleep: MagicMock) -> None:
        """A LIST root with an unresolved tail hash fetches it as one batch."""
        node = _fake_node(fetch_retries=1)
        tail_hash = b"\xcc" * 32
        root = _make_hash_ref_link(ZERO32, tail_hash)
        with patch("astreum.storage.exprs.list.get_expr_list_from_local_storage", return_value=root), \
             patch("astreum.storage.exprs.network.get_exprs_from_network", return_value={}) as holes:
            net._resolve_structured(node, root.hash(), RESOLUTION_LIST)
        holes.assert_called_once_with(node, [(tail_hash, RESOLUTION_SINGLE)])

    @patch("astreum.storage.exprs.network.sleep")
    def test_full_holes_submitted_together(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=1)
        head_hash = b"\xdd" * 32
        tail_hash = b"\xee" * 32
        root = _make_hash_ref_link(head_hash, tail_hash)
        with patch("astreum.storage.exprs.full.get_expr_full_from_local_storage", return_value=root), \
             patch("astreum.storage.exprs.network.get_exprs_from_network", return_value={}) as holes:
            net._resolve_structured(node, root.hash(), RESOLUTION_FULL)
        holes.assert_called_once()
        entries = holes.call_args.args[1]
        self.assertEqual(
            sorted(entries),
            sorted([(head_hash, RESOLUTION_SINGLE), (tail_hash, RESOLUTION_SINGLE)]),
        )

    @patch("astreum.storage.exprs.network.sleep")
    def test_list_poll_no_missing(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=3)
        root = _make_link(int_(1), _make_link(int_(2), NIL))
        with patch(QUEUE, return_value=True), \
             patch("astreum.storage.exprs.list.get_expr_list_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_LIST)
        self.assertEqual(result.hash(), root.hash())

    @patch("astreum.storage.exprs.network.sleep")
    def test_full_poll_no_missing(self, mock_sleep: MagicMock) -> None:
        node = _fake_node(fetch_retries=3)
        root = _make_link(_make_link(int_(1), int_(2)), _make_link(int_(3), int_(4)))
        with patch(QUEUE, return_value=True), \
             patch("astreum.storage.exprs.full.get_expr_full_from_local_storage", return_value=root):
            result = get_expr_from_network(node, root.hash(), RESOLUTION_FULL)
        self.assertEqual(result.hash(), root.hash())

    @patch("astreum.storage.exprs.network.sleep")
    def test_list_root_is_requested_even_if_local(self, mock_sleep: MagicMock) -> None:
        """Only SINGLE short-circuits on a local hit; LIST/FULL may be partial."""
        node = _fake_node(fetch_retries=1)
        root = _make_link(int_(1), NIL)
        with patch(QUEUE, return_value=True) as queue, patch(LOCAL, return_value=root), \
             patch("astreum.storage.exprs.list.get_expr_list_from_local_storage", return_value=root):
            get_expr_from_network(node, root.hash(), RESOLUTION_LIST)
        queue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
