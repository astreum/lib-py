"""STORAGE_GET entries that carry a shape (resolution 0, DYNAMIC)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
for path in (ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.message_pow import MESSAGE_FRAMING_BYTES  # noqa: E402
from astreum.communication.storage_request.code import StorageRequestCode  # noqa: E402
from astreum.communication.storage_request.model import (  # noqa: E402
    StorageRequest,
    get_body_budget,
    get_entry_size,
    max_get_entry_bytes,
)
from astreum.communication.storage_response.code import StorageResponseCode  # noqa: E402
from astreum.communication.storage_response.cull import cull_to_resolution  # noqa: E402
from astreum.communication.storage_response.storage_found import decode_found_page  # noqa: E402
from astreum.expression import (  # noqa: E402
    RESOLUTION_DYNAMIC,
    RESOLUTION_LIST,
    RESOLUTION_SINGLE,
    bytes_,
    exprs_to_linked_expr,
)
from astreum.expression.shape import HOLD, SEND, pair, repeat  # noqa: E402
from astreum.storage.requests import claim_expr_req, get_expr_req_shape  # noqa: E402
from astreum.storage.workers.requests import (  # noqa: E402
    flush_storage_requests,
    queue_storage_get,
)
from astreum.utils.config import config_setup  # noqa: E402
from tests.unit.communication.test_storage_get_batch import TestGetServer  # noqa: E402
from tests.unit.expression.test_shape import make_store  # noqa: E402
from tests.unit.storage.test_request_buffer import _drain, _ids, _node  # noqa: E402

BLOCK = b"\xb1" * 32
DYNAMIC = RESOLUTION_DYNAMIC
SHAPE = repeat(SEND)


def _wire(entries) -> bytes:
    return StorageRequest(StorageRequestCode.STORAGE_GET, entries=entries).to_bytes()


class TestCodec(unittest.TestCase):
    def test_shaped_entry_layout(self):
        eid = _ids(1)[0]
        body = _wire([(eid, DYNAMIC, None, SHAPE)])
        self.assertEqual(body[3 : 3 + 32], eid)
        self.assertEqual(body[3 + 32], DYNAMIC)
        self.assertEqual(body[3 + 33], len(SHAPE))
        self.assertEqual(body[3 + 34 :], SHAPE)

    def test_scoped_shaped_entry_puts_the_shape_after_the_block(self):
        eid = _ids(1)[0]
        body = _wire([(eid, DYNAMIC, BLOCK, SHAPE)])
        self.assertEqual(body[3 + 32], DYNAMIC | 0x80)
        self.assertEqual(body[3 + 33 : 3 + 65], BLOCK)
        self.assertEqual(body[3 + 65], len(SHAPE))
        self.assertEqual(body[3 + 66 :], SHAPE)

    def test_round_trip_of_a_mixed_batch(self):
        a, b, c, d = _ids(4)
        entries = [
            (a, RESOLUTION_SINGLE, None, b""),
            (b, DYNAMIC, None, SHAPE),
            (c, DYNAMIC, BLOCK, pair(SEND, HOLD)),
            (d, RESOLUTION_LIST, BLOCK, b""),
        ]
        decoded = StorageRequest.from_bytes(_wire(entries))
        self.assertEqual(decoded.entries, entries)

    def test_a_128_byte_shape_has_a_two_byte_length(self):
        shape = b"\x03" * 127 + HOLD
        self.assertEqual(len(shape), 128)
        eid = _ids(1)[0]
        body = _wire([(eid, DYNAMIC, None, shape)])
        self.assertEqual(len(body), 3 + 33 + 2 + 128)
        self.assertEqual(StorageRequest.from_bytes(body).entries, [(eid, DYNAMIC, None, shape)])

    def test_encode_rejects_a_bad_shape_or_a_shape_on_another_resolution(self):
        eid = _ids(1)[0]
        for entry in (
            (eid, DYNAMIC, None, b""),
            (eid, DYNAMIC, None, b"\x09"),
            (eid, RESOLUTION_SINGLE, None, SHAPE),
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(ValueError):
                    _wire([entry])

    def test_decode_rejects_a_truncated_or_malformed_shape(self):
        good = _wire([(_ids(1)[0], DYNAMIC, None, SHAPE)])
        for bad in (good[:-1], good + b"\x00"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    StorageRequest.from_bytes(bad)
        malformed = good[:-2] + b"\x09\x09"
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(malformed)

    def test_entry_sizes(self):
        eid = _ids(1)[0]
        self.assertEqual(get_entry_size((eid, RESOLUTION_SINGLE, None, b"")), 33)
        self.assertEqual(get_entry_size((eid, RESOLUTION_SINGLE, BLOCK, b"")), 65)
        self.assertEqual(get_entry_size((eid, DYNAMIC, None, SHAPE)), 33 + 1 + len(SHAPE))
        self.assertEqual(get_entry_size((eid, DYNAMIC, BLOCK, SHAPE)), 65 + 1 + len(SHAPE))
        self.assertEqual(get_entry_size((eid, 1)), 33)

    def test_size_matches_the_encoding(self):
        eid = _ids(1)[0]
        for entry in (
            (eid, DYNAMIC, None, SHAPE),
            (eid, DYNAMIC, BLOCK, b"\x03" * 127 + HOLD),
            (eid, RESOLUTION_LIST, BLOCK, b""),
        ):
            self.assertEqual(len(_wire([entry])) - 3, get_entry_size(entry))


class TestConfig(unittest.TestCase):
    def test_default_shape_limit(self):
        self.assertEqual(config_setup({"default_seed": None}).storage_shape_max_bytes, 128)

    def test_the_default_budget_holds_the_longest_entry_and_more(self):
        longest = max_get_entry_bytes(128)
        self.assertEqual(longest, 65 + 2 + 128)
        self.assertGreaterEqual(get_body_budget(1232), longest + 65)

    def test_a_budget_too_small_for_the_longest_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "storage_get_batch_max_bytes": 300})
        config_setup({"default_seed": None, "storage_get_batch_max_bytes": 300, "storage_shape_max_bytes": 16})

    def test_bad_shape_limit_is_rejected(self):
        for bad in (True, "abc", 0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"default_seed": None, "storage_shape_max_bytes": bad})


class TestBatching(unittest.TestCase):
    def test_a_shaped_entry_among_plain_ones_fits_every_datagram(self):
        node = _node()
        shape = b"\x03" * 127 + HOLD
        ids = _ids(80)
        for index, h in enumerate(ids):
            if index % 10 == 0:
                queue_storage_get(node, h, DYNAMIC, block_hash=BLOCK, shape=shape)
            else:
                queue_storage_get(node, h, RESOLUTION_SINGLE)
        flush_storage_requests(node)
        total = 0
        for _, entries in _drain(node):
            self.assertLessEqual(
                sum(get_entry_size(e) for e in entries), get_body_budget(1232)
            )
            total += len(entries)
        self.assertEqual(total, 80)

    def test_chunks_keep_the_queue_order(self):
        node = _node()
        ids = _ids(70)
        for h in ids:
            queue_storage_get(node, h, RESOLUTION_SINGLE)
        flush_storage_requests(node)
        sent = [e[0] for _, entries in _drain(node) for e in entries]
        self.assertEqual(sent, ids)

    def test_the_shape_is_sent_in_the_entry(self):
        node = _node()
        h = _ids(1)[0]
        queue_storage_get(node, h, DYNAMIC, shape=SHAPE)
        flush_storage_requests(node)
        (_, entries), = _drain(node)
        self.assertEqual(entries, [(h, DYNAMIC, None, SHAPE)])
        self.assertEqual(get_expr_req_shape(node, h), SHAPE)

    def test_a_shaped_datagram_fits_the_message_size(self):
        node = _node()
        for h in _ids(8):
            queue_storage_get(node, h, DYNAMIC, block_hash=BLOCK, shape=b"\x03" * 127 + HOLD)
        flush_storage_requests(node)
        datagrams = _drain(node)
        for _, entries in datagrams:
            body = len(_wire(entries))
            self.assertLessEqual(body + MESSAGE_FRAMING_BYTES, 1232)
        self.assertEqual(sum(len(e) for _, e in datagrams), 8)

    def test_a_long_or_bad_shape_is_refused_when_queued(self):
        node = _node()
        h = _ids(1)[0]
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, DYNAMIC, shape=b"\x03" * 128 + HOLD)
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, DYNAMIC, shape=b"")
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RESOLUTION_SINGLE, shape=SHAPE)
        self.assertTrue(node.outgoing_queue.empty())

    def test_a_second_request_for_the_same_hash_waits_for_the_first(self):
        node = _node()
        h = _ids(1)[0]
        self.assertTrue(claim_expr_req(node, h, DYNAMIC, None, SHAPE))
        self.assertFalse(claim_expr_req(node, h, RESOLUTION_SINGLE))
        self.assertEqual(get_expr_req_shape(node, h), SHAPE)


class TestServe(unittest.TestCase):
    def setUp(self):
        self.items = [bytes_(b"a"), bytes_(b"b"), bytes_(b"c")]
        self.lst = exprs_to_linked_expr(self.items)
        self.store = make_store(self.lst)
        self.helper = TestGetServer("test_mixed_found_redirect_unknown")
        self.helper.setUp()

    def serve(self, entries, local=None):
        return self.helper._serve(entries, local=self.store if local is None else local)

    def exprs_sent(self, responses):
        out = []
        for response in responses:
            self.assertEqual(response.code, StorageResponseCode.STORAGE_FOUND)
            _, _, exprs = decode_found_page(response.data)
            out.extend(exprs)
        return out

    def test_serves_what_the_shape_reaches(self):
        result, responses, _ = self.serve([(self.lst.hash(), DYNAMIC, None, repeat(HOLD))])
        self.assertEqual(result, (True, None))
        sent = self.exprs_sent(responses)
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0].hash(), self.lst.hash())

    def test_a_missing_child_is_left_out(self):
        local = dict(self.store)
        del local[self.items[1].hash()]
        result, responses, _ = self.serve(
            [(self.lst.hash(), DYNAMIC, None, repeat(SEND))], local=local
        )
        self.assertEqual(result, (True, None))
        hashes = {e.hash() for e in self.exprs_sent(responses)}
        self.assertNotIn(self.items[1].hash(), hashes)
        self.assertIn(self.items[0].hash(), hashes)
        self.assertIn(self.items[2].hash(), hashes)

    def test_a_shape_that_selects_nothing_is_refused(self):
        result, responses, _ = self.serve([(self.lst.hash(), DYNAMIC, None, HOLD)])
        self.assertEqual(result, (False, "shape selects nothing"))
        self.assertEqual(responses, [])

    def test_a_shape_longer_than_the_limit_is_refused(self):
        node = self.helper._node()
        node.config.storage_shape_max_bytes = 2
        with patch.object(self.helper, "_node", return_value=node):
            result, responses, _ = self.helper._serve(
                [(self.lst.hash(), DYNAMIC, None, repeat(repeat(SEND)))],
                local=self.store,
                node=node,
            )
        self.assertEqual(result, (False, "shape too long"))
        self.assertEqual(responses, [])


class TestCull(unittest.TestCase):
    def setUp(self):
        self.items = [bytes_(b"a"), bytes_(b"b"), bytes_(b"c")]
        self.lst = exprs_to_linked_expr(self.items)
        self.everything = list(make_store(self.lst).values())

    def test_keeps_only_what_the_shape_reaches(self):
        kept = cull_to_resolution(self.lst, self.everything, DYNAMIC, repeat(HOLD))
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[0].hash(), self.lst.hash())
        heads = {item.hash() for item in self.items}
        self.assertFalse(heads & {e.hash() for e in kept})

    def test_a_wider_shape_keeps_more(self):
        kept = cull_to_resolution(self.lst, self.everything, DYNAMIC, repeat(SEND))
        self.assertEqual(len(kept), 6)

    def test_a_shape_that_does_not_parse_keeps_the_root(self):
        kept = cull_to_resolution(self.lst, self.everything, DYNAMIC, b"\x09")
        self.assertEqual([e.hash() for e in kept], [self.lst.hash()])

    def test_a_shape_that_holds_the_root_still_keeps_it(self):
        kept = cull_to_resolution(self.lst, self.everything, DYNAMIC, HOLD)
        self.assertEqual([e.hash() for e in kept], [self.lst.hash()])


if __name__ == "__main__":
    unittest.main()
