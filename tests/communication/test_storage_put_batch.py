import queue
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.communication.message_pow import (
    MAX_INLINE_MESSAGE_BYTES,
    MAX_UDP_DATAGRAM_BYTES,
    MESSAGE_FRAMING_BYTES,
)
from astreum.communication.models.message import Message, MessageTopic
from astreum.communication.models.route import Route
from astreum.communication.storage_request import handle as put_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import (
    StorageRequest,
    max_batch_entries,
)
from astreum.storage.advertisements import advertise_exprs
from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES, config_setup

SELF_KEY = b"\x00" * 32
PEER_A_KEY = b"\xff" * 32
PEER_B_KEY = b"\x80" + b"\x00" * 31
PROVIDER = b"P" * 70


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


def _peer(key: bytes, port: int):
    return SimpleNamespace(
        address=("127.0.0.1", port),
        public_key_bytes=key,
        shared_key_bytes=b"\x01" * 32,
        difficulty=1,
    )


def _make_node(peers=(), budget=DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES):
    route = Route(SELF_KEY)
    for peer in peers:
        route.add_peer(peer.public_key_bytes, peer)
    return SimpleNamespace(
        config={
            "storage_public_key_bytes": SELF_KEY,
            "relay_public_key_bytes": b"R" * 32,
            "port": 52780,
            "storage_put_batch_max_bytes": budget,
        },
        storage_public_key_bytes=SELF_KEY,
        relay_ip_address="10.0.0.1",
        logger=_silent_logger(),
        peer_route=route,
        outgoing_queue=queue.Queue(),
        storage_index={},
        storage_providers=[],
        long_term_storage=False,
    )


def _ids(prefix: int, n: int, start: int = 0) -> list[bytes]:
    """n distinct 32-byte ids whose first byte is *prefix* (so xor distance to
    the 0x00 self key / 0xFF peer key is controlled by the prefix)."""
    return [bytes([prefix]) + (start + i).to_bytes(31, "big") for i in range(n)]


def _sent_requests(node, shared_key=b"\x01" * 32):
    """Drain the outgoing queue and decode every STORAGE_PUT sent."""
    out = []
    while not node.outgoing_queue.empty():
        payload, address = node.outgoing_queue.get_nowait()
        message = Message.from_bytes(payload[8:])
        message.decrypt(shared_key)
        assert message.topic == MessageTopic.STORAGE_REQUEST
        out.append((address, StorageRequest.from_bytes(message.content)))
    return out


class TestFramingConstant(unittest.TestCase):
    def test_framing_includes_timestamp(self):
        self.assertEqual(MESSAGE_FRAMING_BYTES, 78)
        self.assertEqual(MAX_INLINE_MESSAGE_BYTES, MAX_UDP_DATAGRAM_BYTES - 78)

    def test_max_inline_content_fits_one_datagram(self):
        message = Message(
            topic=MessageTopic.STORAGE_REQUEST,
            content=b"x" * MAX_INLINE_MESSAGE_BYTES,
            sender_public_key_bytes=b"S" * 32,
        )
        message.encrypt(b"\x01" * 32)
        wire = 8 + len(message.to_bytes())  # + PoW nonce
        self.assertLessEqual(wire, MAX_UDP_DATAGRAM_BYTES)


class TestCodec(unittest.TestCase):
    def _req(self, n):
        entries = [(_ids(0xFF, n)[i], 4) for i in range(n)]
        return StorageRequest(StorageRequestCode.STORAGE_PUT, PROVIDER, entries=entries)

    def test_round_trip(self):
        req = self._req(5)
        decoded = StorageRequest.from_bytes(req.to_bytes())
        self.assertEqual(decoded.code, StorageRequestCode.STORAGE_PUT)
        self.assertEqual(decoded.data, PROVIDER)
        self.assertEqual(decoded.entries, req.entries)

    def test_batch_of_one_size(self):
        self.assertEqual(len(self._req(1).to_bytes()), 1 + 70 + 2 + 33)

    def test_rejects_zero_count(self):
        raw = bytes([StorageRequestCode.STORAGE_PUT]) + PROVIDER + (0).to_bytes(2, "big")
        raw += b"\x00" * 33  # pad past the generic minimum length
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw)

    def test_rejects_length_count_mismatch(self):
        raw = self._req(3).to_bytes()
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw[:-1])
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw + b"\x00")

    def test_rejects_short_payload(self):
        raw = bytes([StorageRequestCode.STORAGE_PUT]) + PROVIDER[:40]
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw)

    def test_rejects_oversized_payload(self):
        raw = bytes([StorageRequestCode.STORAGE_PUT]) + b"\x00" * MAX_INLINE_MESSAGE_BYTES
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(raw)

    def test_encode_rejects_empty_and_bad_provider(self):
        with self.assertRaises(ValueError):
            StorageRequest(StorageRequestCode.STORAGE_PUT, PROVIDER, entries=[]).to_bytes()
        with self.assertRaises(ValueError):
            StorageRequest(
                StorageRequestCode.STORAGE_PUT, b"short", entries=[(b"\x00" * 32, 1)]
            ).to_bytes()

    def test_get_roundtrip(self):
        entries = [(b"\x07" * 32, 2), (b"\x08" * 32, 0)]
        req = StorageRequest(StorageRequestCode.STORAGE_GET, entries=entries)
        decoded = StorageRequest.from_bytes(req.to_bytes())
        self.assertEqual(decoded.code, StorageRequestCode.STORAGE_GET)
        self.assertEqual(decoded.entries, entries)


class TestBudgetConfig(unittest.TestCase):
    def test_default_entries(self):
        self.assertEqual(DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES, 1232)
        self.assertEqual(max_batch_entries(1232), 32)
        config = config_setup({"default_seed": None})
        self.assertEqual(config["storage_put_batch_max_bytes"], 1232)

    def test_full_batch_fits_budget(self):
        entries = [(e, 1) for e in _ids(0xFF, 32)]
        body = StorageRequest(StorageRequestCode.STORAGE_PUT, PROVIDER, entries=entries).to_bytes()
        self.assertLessEqual(len(body) + MESSAGE_FRAMING_BYTES, 1232)

    def test_custom_budget(self):
        config = config_setup({"default_seed": None, "storage_put_batch_max_bytes": 2000})
        self.assertEqual(config["storage_put_batch_max_bytes"], 2000)
        self.assertGreater(max_batch_entries(2000), max_batch_entries(1232))

    def test_invalid_budgets(self):
        for bad in ("abc", True, MAX_UDP_DATAGRAM_BYTES + 1, 100, 0, -5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"default_seed": None, "storage_put_batch_max_bytes": bad})


class TestSendPath(unittest.TestCase):
    def setUp(self):
        self.peer_a = _peer(PEER_A_KEY, 9001)
        self.peer_b = _peer(PEER_B_KEY, 9002)

    def _entries(self, ids):
        return [(e, 4, None) for e in ids]

    def test_split_by_budget(self):
        node = _make_node([self.peer_a])
        ids = _ids(0xFF, 70)
        advertised, warning = advertise_exprs(node, self._entries(ids))
        self.assertIsNone(warning)
        self.assertEqual(advertised, ids)
        sent = _sent_requests(node)
        self.assertEqual([len(r.entries) for _, r in sent], [32, 32, 6])
        self.assertTrue(all(addr == self.peer_a.address for addr, _ in sent))
        self.assertEqual([e for _, r in sent for e, _ in r.entries], ids)
        self.assertTrue(all(r.data == node_provider(node) for _, r in sent))

    def test_custom_budget_changes_chunk_size(self):
        node = _make_node([self.peer_a], budget=78 + 1 + 72 + 33 * 4)
        advertise_exprs(node, self._entries(_ids(0xFF, 10)))
        self.assertEqual([len(r.entries) for _, r in _sent_requests(node)], [4, 4, 2])

    def test_buckets_by_destination(self):
        node = _make_node([self.peer_a, self.peer_b])
        to_a = _ids(0xFF, 3)
        to_b = _ids(0x80, 2)
        advertise_exprs(node, self._entries(to_a + to_b))
        by_addr = {}
        for addr, req in _sent_requests(node):
            by_addr.setdefault(addr, []).extend(e for e, _ in req.entries)
        self.assertEqual(by_addr[self.peer_a.address], to_a)
        self.assertEqual(by_addr[self.peer_b.address], to_b)

    def test_one_expr_is_batch_of_one(self):
        node = _make_node([self.peer_a])
        eid = _ids(0xFF, 1)[0]
        advertise_exprs(node, self._entries([eid]))
        sent = _sent_requests(node)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].entries, [(eid, 4)])

    def test_self_closest_is_indexed_not_sent(self):
        node = _make_node([self.peer_a])
        mine = _ids(0x00, 3)
        theirs = _ids(0xFF, 2)
        advertised, _ = advertise_exprs(node, self._entries(mine + theirs))
        self.assertCountEqual(advertised, mine + theirs)
        self.assertEqual(set(node.storage_index), set(mine))
        sent = _sent_requests(node)
        self.assertEqual([e for _, r in sent for e, _ in r.entries], theirs)

    def test_no_peers_indexes_everything(self):
        node = _make_node([])
        ids = _ids(0xFF, 4)
        advertised, warning = advertise_exprs(node, self._entries(ids))
        self.assertIsNone(warning)
        self.assertEqual(set(node.storage_index), set(ids))
        self.assertTrue(node.outgoing_queue.empty())

    def test_enqueue_failure_marks_whole_batch_failed(self):
        node = _make_node([self.peer_a])
        ids = _ids(0xFF, 3)
        with patch(
            "astreum.communication.outgoing_queue.enqueue_outgoing", return_value=False
        ):
            advertised, warning = advertise_exprs(node, self._entries(ids))
        self.assertEqual(advertised, [])
        self.assertIn("3 advertisement(s) failed", warning)

    def test_expired_entries_filtered(self):
        node = _make_node([self.peer_a])
        ids = _ids(0xFF, 2)
        entries = [(ids[0], 4, 1.0), (ids[1], 4, None)]
        advertised, _ = advertise_exprs(node, entries)
        self.assertEqual(advertised, [ids[1]])


def node_provider(node) -> bytes:
    from astreum.storage.exprs.network import build_provider_payload

    return build_provider_payload(node)


class TestReceivePath(unittest.TestCase):
    def setUp(self):
        self.peer_a = _peer(PEER_A_KEY, 9001)
        self.sender = _peer(b"\x55" * 32, 9100)

    def _handle(self, node, entries, committed=None, record_ids=None):
        """Run the STORAGE_PUT handler with admission/record checks stubbed."""
        committed = set(e for e, _ in entries) if committed is None else set(committed)
        record_ids = committed if record_ids is None else set(record_ids)
        request = StorageRequest(StorageRequestCode.STORAGE_PUT, PROVIDER, entries=entries)
        message = SimpleNamespace(content=request.to_bytes())
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x09" * 32))
        with patch(
            "astreum.storage.admission.is_expr_in_latest_block",
            side_effect=lambda n, e: e in committed,
        ), patch(
            "astreum.storage.admission.get_latest_storage_account", return_value=account
        ), patch(
            "astreum.storage.radix.get_from_radix_tree",
            side_effect=lambda tree, n, e: e,
        ), patch(
            "astreum.storage.records.parse_record_new_count",
            side_effect=lambda n, v: 1 if v in record_ids else None,
        ), patch.object(
            put_handle, "get_record_from_cold_storage", return_value=None
        ), patch(
            "astreum.storage.records.fetch_and_store_record", return_value=True
        ) as fetch:
            result = put_handle.handle_storage_request(node, self.sender, message)
        return result, fetch

    def test_admission_failure_does_not_fail_batch(self):
        node = _make_node([self.peer_a])
        ids = _ids(0x00, 4)
        entries = [(e, 4) for e in ids]
        result, _ = self._handle(node, entries, committed=ids[:2])
        self.assertEqual(result, (True, None))
        self.assertEqual(set(node.storage_index), set(ids[:2]))

    def test_all_rejected_reports_failure(self):
        node = _make_node([self.peer_a])
        entries = [(e, 4) for e in _ids(0x00, 2)]
        (ok, reason), _ = self._handle(node, entries, committed=[])
        self.assertFalse(ok)
        self.assertIn("no exprs committed", reason)
        self.assertEqual(node.storage_index, {})

    def test_non_record_exprs_skipped(self):
        node = _make_node([self.peer_a])
        ids = _ids(0x00, 3)
        result, _ = self._handle(node, [(e, 4) for e in ids], record_ids=ids[:1])
        self.assertEqual(result, (True, None))
        self.assertEqual(set(node.storage_index), set(ids[:1]))

    def test_self_indexing_records_provider(self):
        node = _make_node([self.peer_a])
        ids = _ids(0x00, 2)
        self._handle(node, [(e, 4) for e in ids])
        self.assertEqual(node.storage_providers, [PROVIDER])
        self.assertEqual(set(node.storage_index.values()), {0})

    def test_inline_fetch_capped_at_eight(self):
        node = _make_node([self.peer_a])
        node.long_term_storage = True
        ids = _ids(0x00, 12)
        with patch("astreum.storage.radix.RadixTree"):
            _, fetch = self._handle(node, [(e, 4) for e in ids])
        self.assertEqual(fetch.call_count, put_handle.BATCH_INLINE_FETCH_LIMIT)
        self.assertEqual(len(node.storage_index), 12)

    def test_no_inline_fetch_without_long_term_storage(self):
        node = _make_node([self.peer_a])
        _, fetch = self._handle(node, [(e, 4) for e in _ids(0x00, 3)])
        self.assertEqual(fetch.call_count, 0)

    def test_forward_regroups_by_destination(self):
        peer_b = _peer(PEER_B_KEY, 9002)
        node = _make_node([self.peer_a, peer_b])
        to_a = _ids(0xFF, 3)
        to_b = _ids(0x80, 2)
        mine = _ids(0x00, 1)
        result, _ = self._handle(node, [(e, 4) for e in to_a + to_b + mine])
        self.assertEqual(result, (True, None))
        self.assertEqual(set(node.storage_index), set(mine))
        sent = _sent_requests(node)
        self.assertEqual(len(sent), 2)
        by_addr = {addr: [e for e, _ in r.entries] for addr, r in sent}
        self.assertEqual(by_addr[self.peer_a.address], to_a)
        self.assertEqual(by_addr[peer_b.address], to_b)
        self.assertTrue(all(r.data == PROVIDER for _, r in sent))

    def test_forward_chunks_by_budget(self):
        node = _make_node([self.peer_a], budget=78 + 1 + 72 + 33 * 3)
        ids = _ids(0xFF, 7)
        self._handle(node, [(e, 4) for e in ids])
        self.assertEqual([len(r.entries) for _, r in _sent_requests(node)], [3, 3, 1])

    def test_malformed_payload_rejected(self):
        node = _make_node([self.peer_a])
        good = StorageRequest(
            StorageRequestCode.STORAGE_PUT, PROVIDER, entries=[(_ids(0, 1)[0], 4)]
        ).to_bytes()
        message = SimpleNamespace(content=good[:-3])
        result = put_handle.handle_storage_request(node, self.sender, message)
        self.assertEqual(result, (False, "decode failed"))


if __name__ == "__main__":
    unittest.main()
