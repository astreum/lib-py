import queue
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.communication.message_pow import MAX_INLINE_MESSAGE_BYTES, MAX_UDP_DATAGRAM_BYTES
from astreum.communication.storage_request import handle as req_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_found import (
    PAGE_ENTRY_BUDGET,
    STORAGE_FOUND_PAYLOAD,
    decode_found_page,
    encode_found_pages,
    found_page_expr_bytes,
)
from astreum.expression import NIL, RESOLUTION_FULL, bytes_, link
from astreum.expression.encoding import encode_expr_to_bytes

BIG = 30000


def _big(i: int):
    return bytes_(bytes([i]) * BIG)


def _root():
    return link(bytes_(b"root"), NIL)


def _header(page, total, count):
    return (
        bytes([STORAGE_FOUND_PAYLOAD])
        + page.to_bytes(2, "big")
        + total.to_bytes(2, "big")
        + count.to_bytes(2, "big")
    )


class TestCodec(unittest.TestCase):
    def test_roundtrip_and_page_numbers(self):
        exprs = [_root()] + [_big(i) for i in range(1, 6)]
        pages = encode_found_pages(exprs)
        self.assertGreater(len(pages), 1)
        decoded = []
        for i, payload in enumerate(pages, start=1):
            self.assertEqual(payload[0], STORAGE_FOUND_PAYLOAD)
            page, total, es = decode_found_page(payload)
            self.assertEqual((page, total), (i, len(pages)))
            self.assertLessEqual(len(payload), PAGE_ENTRY_BUDGET + 7)
            self.assertLessEqual(33 + len(payload), MAX_INLINE_MESSAGE_BYTES)
            decoded.append(es)
        self.assertEqual(decoded[0][0].hash(), exprs[0].hash())
        for es in decoded[1:]:
            self.assertNotIn(exprs[0].hash(), [e.hash() for e in es])
        got = [e.hash() for es in decoded for e in es]
        self.assertEqual(got, [e.hash() for e in exprs])

    def test_small_reply_is_page_one_of_one(self):
        pages = encode_found_pages([_root(), bytes_(b"small")])
        self.assertEqual(len(pages), 1)
        self.assertEqual(decode_found_page(pages[0])[:2], (1, 1))

    def test_header_layout(self):
        c = encode_found_pages([_root()])[0]
        self.assertEqual(c[0], 1)
        self.assertEqual(int.from_bytes(c[1:3], "big"), 1)
        self.assertEqual(int.from_bytes(c[3:5], "big"), 1)
        self.assertEqual(int.from_bytes(c[5:7], "big"), 1)
        self.assertEqual(found_page_expr_bytes(c), len(encode_expr_to_bytes(_root())))

    def test_oversized_child_skipped_root_oversized_raises(self):
        huge = bytes_(b"h" * (PAGE_ENTRY_BUDGET + 10))
        skipped = []
        pages = encode_found_pages(
            [_root(), _big(1), huge], on_skip=lambda e, n: skipped.append(e.hash())
        )
        self.assertEqual(skipped, [huge.hash()])
        got = {e.hash() for p in pages for e in decode_found_page(p)[2]}
        self.assertNotIn(huge.hash(), got)
        self.assertIn(_big(1).hash(), got)
        with self.assertRaises(ValueError):
            encode_found_pages([huge, _big(1)])

    def test_decode_rejects_garbage(self):
        good = encode_found_pages([_root()])[0]
        entry = good[7:]
        cases = {
            "trailing": good + b"\x00",
            "truncated": good[:-1],
            "short header": good[:5],
            "wrong type": b"\x02" + good[1:],
            "page zero": _header(0, 1, 1) + entry,
            "page above total": _header(2, 1, 1) + entry,
            "total zero": _header(1, 0, 1) + entry,
            "count zero": _header(1, 1, 0),
            "zero-length expr": _header(1, 1, 1) + (0).to_bytes(4, "big"),
        }
        for name, payload in cases.items():
            with self.assertRaises(ValueError, msg=name):
                decode_found_page(payload)

    def test_no_exprs_raises(self):
        with self.assertRaises(ValueError):
            encode_found_pages([])


class TestServer(unittest.TestCase):
    def _run(self, exprs, config=None):
        node = SimpleNamespace(
            logger=MagicMock(),
            storage_public_key_bytes=b"\x00" * 32,
            storage_index={},
            outgoing_queue=queue.Queue(),
            config=config if config is not None else {},
        )
        peer = SimpleNamespace(
            address=("127.0.0.1", 1), shared_key_bytes=b"\x01" * 32, difficulty=1,
            metrics={},
        )
        req = StorageRequest(
            code=StorageRequestCode.STORAGE_GET,
            entries=[(exprs[0].hash(), RESOLUTION_FULL)],
        )
        message = SimpleNamespace(content=req.to_bytes())
        sent = []

        def fake_enqueue(node_, address, message, difficulty=1):
            sent.append(message)
            return True

        uploads = []
        with patch.object(req_handle, "get_expr_from_local_storage", return_value=exprs[0]), \
             patch.object(req_handle, "_collect_for_resolution", return_value=exprs), \
             patch.object(req_handle, "_requires_storage_channel", return_value=False), \
             patch.object(req_handle, "enqueue_outgoing", side_effect=fake_enqueue), \
             patch.object(req_handle, "increment_peer_metric",
                          side_effect=lambda p, k, v: uploads.append((k, v))):
            result = req_handle.handle_storage_request(node, peer, message)
        return node, result, sent, uploads

    def _payload_of(self, msg):
        return StorageResponse.from_bytes(msg.content).data

    def test_small_response_is_one_page(self):
        exprs = [_root(), bytes_(b"small")]
        _, result, sent, uploads = self._run(exprs)
        self.assertEqual(result, (True, None))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self._payload_of(sent[0]), encode_found_pages(exprs)[0])
        self.assertEqual(decode_found_page(self._payload_of(sent[0]))[:2], (1, 1))

    def test_large_response_is_paged_in_order(self):
        exprs = [_root()] + [_big(i) for i in range(1, 8)]
        _, result, sent, uploads = self._run(exprs)
        self.assertEqual(result, (True, None))
        self.assertGreater(len(sent), 1)
        for i, m in enumerate(sent, start=1):
            data = self._payload_of(m)
            self.assertLessEqual(33 + len(data), MAX_INLINE_MESSAGE_BYTES)
            self.assertLessEqual(8 + len(m.to_bytes()), MAX_UDP_DATAGRAM_BYTES)
            self.assertEqual(decode_found_page(data)[:2], (i, len(sent)))
        expected = sum(len(encode_expr_to_bytes(e)) for e in exprs)
        self.assertEqual(sum(v for k, v in uploads if k == "shared_storage_upload"), expected)

    def test_oversized_root_sends_nothing(self):
        huge = bytes_(b"h" * (PAGE_ENTRY_BUDGET + 10))
        node, result, sent, _ = self._run([huge])
        self.assertFalse(result[0])
        self.assertEqual(sent, [])
        node.logger.error.assert_called()

    def test_more_pages_than_configured_limit_sends_nothing(self):
        exprs = [_root()] + [_big(i) for i in range(1, 8)]
        node, result, sent, uploads = self._run(exprs, config={"storage_found_max_pages": 2})
        self.assertEqual(result, (False, "response too large"))
        self.assertEqual(sent, [])
        self.assertEqual(uploads, [])
        node.logger.error.assert_called()

    def test_limit_is_read_from_config(self):
        exprs = [_root()] + [_big(i) for i in range(1, 8)]
        _, _, default_sent, _ = self._run(exprs)
        pages = len(default_sent)
        _, result, sent, _ = self._run(exprs, config={"storage_found_max_pages": pages})
        self.assertEqual(result, (True, None))
        self.assertEqual(len(sent), pages)


if __name__ == "__main__":
    unittest.main()
