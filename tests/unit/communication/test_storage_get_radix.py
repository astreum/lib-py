"""STORAGE_GET entries of the radix kinds (resolution 4 RADIX_KEY, 5 RADIX_ALL)."""

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
from astreum.communication.storage_request import handle as req_handle  # noqa: E402
from astreum.communication.storage_request.code import StorageRequestCode  # noqa: E402
from astreum.communication.storage_request.model import (  # noqa: E402
    StorageRequest,
    encode_radix_all_param,
    encode_radix_key_param,
    get_body_budget,
    get_entry_size,
    max_get_entry_bytes,
    parse_radix_all_param,
    parse_radix_key_param,
)
from astreum.communication.storage_response.code import StorageResponseCode  # noqa: E402
from astreum.communication.storage_response.cull import cull_to_resolution  # noqa: E402
from astreum.communication.storage_response.storage_found import (  # noqa: E402
    PAGE_ENTRY_BUDGET,
    decode_found_page,
)
from astreum.expression import (  # noqa: E402
    RESOLUTION_DYNAMIC,
    RESOLUTION_RADIX_ALL,
    RESOLUTION_RADIX_KEY,
    RESOLUTION_SINGLE,
    bytes_,
    exprs_to_linked_expr,
)
from astreum.expression.shape import HOLD, SEND, pair, repeat  # noqa: E402
from astreum.storage.requests import get_expr_req_shape  # noqa: E402
from astreum.storage.workers.requests import (  # noqa: E402
    flush_storage_requests,
    queue_storage_get,
)
from astreum.utils.config import config_setup  # noqa: E402
from tests.unit.communication.test_storage_get_batch import TestGetServer  # noqa: E402
from tests.unit.expression.test_shape import make_store  # noqa: E402
from tests.unit.storage.test_request_buffer import _drain, _ids, _node  # noqa: E402

BLOCK = b"\xb1" * 32
RADIX = RESOLUTION_RADIX_KEY
RADIX_ALL = RESOLUTION_RADIX_ALL
VALUE_SHAPE = repeat(SEND)
KEY = bytes(range(32))


def _wire(entries) -> bytes:
    return StorageRequest(StorageRequestCode.STORAGE_GET, entries=entries).to_bytes()


class TestParam(unittest.TestCase):
    def test_layout(self):
        self.assertEqual(encode_radix_key_param(5, b"\xaa\xbb"), b"\x05\x02\xaa\xbb\x00")

    def test_layout_with_a_value_shape(self):
        self.assertEqual(
            encode_radix_key_param(5, b"\xaa\xbb", VALUE_SHAPE),
            b"\x05\x02\xaa\xbb\x02" + VALUE_SHAPE,
        )
        self.assertEqual(encode_radix_all_param(VALUE_SHAPE), b"\x02" + VALUE_SHAPE)
        self.assertEqual(encode_radix_all_param(), b"\x00")

    def test_round_trip(self):
        for offset in (0, 1, 127, 128, 255):
            with self.subTest(offset=offset):
                self.assertEqual(
                    parse_radix_key_param(encode_radix_key_param(offset, KEY, VALUE_SHAPE)),
                    (offset, KEY, VALUE_SHAPE),
                )
                self.assertEqual(
                    parse_radix_key_param(encode_radix_key_param(offset, KEY)), (offset, KEY, b"")
                )

    def test_round_trip_of_the_all_param(self):
        for value_shape in (b"", SEND, VALUE_SHAPE):
            with self.subTest(value_shape=value_shape):
                self.assertEqual(
                    parse_radix_all_param(encode_radix_all_param(value_shape)), value_shape
                )

    def test_an_offset_past_the_end_of_the_key_is_refused(self):
        with self.assertRaises(ValueError):
            encode_radix_key_param(257, KEY)
        with self.assertRaises(ValueError):
            encode_radix_key_param(-1, KEY)
        with self.assertRaises(ValueError):
            parse_radix_key_param(b"\x09\x01\xaa")

    def test_a_cut_or_long_param_is_refused(self):
        good = encode_radix_key_param(3, KEY, VALUE_SHAPE)
        for bad in (b"", good[:-1], good + b"\x00"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    parse_radix_key_param(bad)
        good = encode_radix_all_param(VALUE_SHAPE)
        for bad in (b"", good[:-1], good + b"\x00"):
            with self.subTest(all_length=len(bad)):
                with self.assertRaises(ValueError):
                    parse_radix_all_param(bad)

    def test_a_value_shape_that_does_not_parse_is_refused(self):
        with self.assertRaises(ValueError):
            encode_radix_key_param(0, KEY, b"\x09")
        with self.assertRaises(ValueError):
            encode_radix_all_param(b"\x09")
        with self.assertRaises(ValueError):
            parse_radix_key_param(encode_radix_key_param(0, KEY)[:-1] + b"\x01\x09")
        with self.assertRaises(ValueError):
            parse_radix_all_param(b"\x01\x09")


class TestCodec(unittest.TestCase):
    def test_entry_layout(self):
        eid = _ids(1)[0]
        param = encode_radix_key_param(9, KEY)
        body = _wire([(eid, RADIX, None, param)])
        self.assertEqual(body[3 : 3 + 32], eid)
        self.assertEqual(body[3 + 32], RADIX)
        self.assertEqual(body[3 + 33 :], param)

    def test_scoped_entry_puts_the_param_after_the_block(self):
        eid = _ids(1)[0]
        param = encode_radix_key_param(9, KEY)
        body = _wire([(eid, RADIX, BLOCK, param)])
        self.assertEqual(body[3 + 32], RADIX | 0x80)
        self.assertEqual(body[3 + 33 : 3 + 65], BLOCK)
        self.assertEqual(body[3 + 65 :], param)

    def test_round_trip_of_a_mixed_batch(self):
        a, b, c, d, e, f = _ids(6)
        entries = [
            (a, RESOLUTION_SINGLE, None, b""),
            (b, RADIX, None, encode_radix_key_param(0, KEY)),
            (c, RADIX, BLOCK, encode_radix_key_param(40, KEY, VALUE_SHAPE)),
            (d, RESOLUTION_DYNAMIC, BLOCK, repeat(SEND)),
            (e, RADIX_ALL, BLOCK, encode_radix_all_param(VALUE_SHAPE)),
            (f, RADIX_ALL, None, encode_radix_all_param()),
        ]
        self.assertEqual(StorageRequest.from_bytes(_wire(entries)).entries, entries)

    def test_encode_rejects_a_bad_param(self):
        eid = _ids(1)[0]
        for param in (b"", b"\x01", encode_radix_key_param(0, KEY) + b"\x00"):
            with self.subTest(param=param):
                with self.assertRaises(ValueError):
                    _wire([(eid, RADIX, None, param)])
        for param in (b"", b"\x01", encode_radix_all_param() + b"\x00"):
            with self.subTest(all_param=param):
                with self.assertRaises(ValueError):
                    _wire([(eid, RADIX_ALL, None, param)])

    def test_all_entry_layout(self):
        eid = _ids(1)[0]
        param = encode_radix_all_param(VALUE_SHAPE)
        body = _wire([(eid, RADIX_ALL, BLOCK, param)])
        self.assertEqual(body[3 + 32], RADIX_ALL | 0x80)
        self.assertEqual(body[3 + 33 : 3 + 65], BLOCK)
        self.assertEqual(body[3 + 65 :], param)

    def test_decode_rejects_a_cut_entry(self):
        good = _wire([(_ids(1)[0], RADIX, None, encode_radix_key_param(0, KEY))])
        for bad in (good[:-1], good + b"\x00"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    StorageRequest.from_bytes(bad)

    def test_decode_rejects_an_offset_past_the_key(self):
        eid = _ids(1)[0]
        body = bytearray(_wire([(eid, RADIX, None, encode_radix_key_param(0, b"\xaa"))]))
        body[3 + 33] = 9
        with self.assertRaises(ValueError):
            StorageRequest.from_bytes(bytes(body))

    def test_decode_rejects_a_cut_all_entry(self):
        good = _wire([(_ids(1)[0], RADIX_ALL, None, encode_radix_all_param(VALUE_SHAPE))])
        for bad in (good[:-1], good + b"\x00"):
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    StorageRequest.from_bytes(bad)

    def test_entry_size_matches_the_encoding(self):
        eid = _ids(1)[0]
        param = encode_radix_key_param(200, KEY, VALUE_SHAPE)
        all_param = encode_radix_all_param(VALUE_SHAPE)
        for entry in (
            (eid, RADIX, None, param),
            (eid, RADIX, BLOCK, param),
            (eid, RADIX_ALL, None, all_param),
            (eid, RADIX_ALL, BLOCK, all_param),
            (eid, RESOLUTION_SINGLE, BLOCK, b""),
        ):
            self.assertEqual(len(_wire([entry])) - 3, get_entry_size(entry))


class TestConfig(unittest.TestCase):
    def test_default_key_limit(self):
        self.assertEqual(config_setup({"default_seed": None}).storage_radix_key_max_bytes, 32)

    def test_the_longest_entry_covers_a_scoped_radix_entry(self):
        eid = _ids(1)[0]
        value_shape = HOLD
        for _ in range(8):
            value_shape = pair(SEND, value_shape)
        self.assertEqual(len(value_shape), 17)
        entry = (eid, RADIX, BLOCK, encode_radix_key_param(255, KEY, value_shape))
        self.assertEqual(max_get_entry_bytes(17, 32), get_entry_size(entry))
        all_entry = (eid, RADIX_ALL, BLOCK, encode_radix_all_param(value_shape))
        self.assertGreaterEqual(max_get_entry_bytes(17, 32), get_entry_size(all_entry))

    def test_the_default_budget_holds_a_radix_entry_and_more(self):
        self.assertGreaterEqual(get_body_budget(1232), max_get_entry_bytes(128, 32) + 65)

    def test_a_budget_too_small_for_a_long_key_is_rejected(self):
        small = {"default_seed": None, "storage_get_batch_max_bytes": 300, "storage_shape_max_bytes": 16}
        config_setup(small)
        with self.assertRaises(ValueError):
            config_setup({**small, "storage_radix_key_max_bytes": 200})

    def test_bad_key_limit_is_rejected(self):
        for bad in (True, "abc", 0, -1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"default_seed": None, "storage_radix_key_max_bytes": bad})


class TestBatching(unittest.TestCase):
    def test_radix_entries_among_plain_ones_fit_every_datagram(self):
        node = _node()
        param = encode_radix_key_param(255, KEY, VALUE_SHAPE)
        all_param = encode_radix_all_param(VALUE_SHAPE)
        ids = _ids(80)
        for index, h in enumerate(ids):
            if index % 10 == 0:
                queue_storage_get(node, h, RADIX, block_hash=BLOCK, shape=param)
            elif index % 10 == 5:
                queue_storage_get(node, h, RADIX_ALL, block_hash=BLOCK, shape=all_param)
            else:
                queue_storage_get(node, h, RESOLUTION_SINGLE)
        flush_storage_requests(node)
        total = 0
        for _, entries in _drain(node):
            self.assertLessEqual(sum(get_entry_size(e) for e in entries), get_body_budget(1232))
            self.assertLessEqual(len(_wire(entries)) + MESSAGE_FRAMING_BYTES, 1232)
            total += len(entries)
        self.assertEqual(total, 80)

    def test_the_param_is_sent_in_the_entry(self):
        node = _node()
        h = _ids(1)[0]
        param = encode_radix_key_param(4, KEY)
        queue_storage_get(node, h, RADIX, shape=param)
        flush_storage_requests(node)
        (_, entries), = _drain(node)
        self.assertEqual(entries, [(h, RADIX, None, param)])
        self.assertEqual(get_expr_req_shape(node, h), param)

    def test_the_all_param_is_sent_in_the_entry(self):
        node = _node()
        h = _ids(1)[0]
        param = encode_radix_all_param(VALUE_SHAPE)
        queue_storage_get(node, h, RADIX_ALL, block_hash=BLOCK, shape=param)
        flush_storage_requests(node)
        (_, entries), = _drain(node)
        self.assertEqual(entries, [(h, RADIX_ALL, BLOCK, param)])
        self.assertEqual(get_expr_req_shape(node, h), param)

    def test_a_long_value_shape_is_refused_when_queued(self):
        node = _node()
        h = _ids(1)[0]
        limit = node.config.storage_shape_max_bytes
        long_shape = HOLD
        for _ in range(limit // 2 + 1):
            long_shape = pair(SEND, long_shape)
        self.assertGreater(len(long_shape), limit)
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RADIX_ALL, shape=encode_radix_all_param(long_shape))
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RADIX, shape=encode_radix_key_param(0, KEY, long_shape))
        self.assertTrue(node.outgoing_queue.empty())

    def test_a_bad_all_param_is_refused_when_queued(self):
        node = _node()
        h = _ids(1)[0]
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RADIX_ALL, shape=b"")
        self.assertTrue(node.outgoing_queue.empty())

    def test_a_bad_or_long_key_is_refused_when_queued(self):
        node = _node()
        h = _ids(1)[0]
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RADIX, shape=b"")
        with self.assertRaises(ValueError):
            queue_storage_get(node, h, RADIX, shape=encode_radix_key_param(0, b"\x01" * 33))
        self.assertTrue(node.outgoing_queue.empty())


class TestRetry(unittest.TestCase):
    def test_a_redirect_retry_keeps_the_key_and_offset(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        from astreum.communication.models.relay_endpoint import RelayEndpoint
        from astreum.communication.storage_response.retry import (
            _retry_pending_storage_get_via_peer_contact,
        )
        from astreum.storage.requests import claim_expr_req

        provider = X25519PrivateKey.generate()
        relay = provider.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        node = _node()
        h = _ids(1)[0]
        param = encode_radix_key_param(17, KEY)
        claim_expr_req(node, h, RADIX, BLOCK, param)
        self.assertTrue(
            _retry_pending_storage_get_via_peer_contact(
                node, expr_id=h, peer_contact=RelayEndpoint(relay, ("10.1.1.1", 6000))
            )
        )
        shared = provider.exchange(node.relay_secret_key.public_key())
        flush_storage_requests(node)
        (_, entries), = _drain(node, shared)
        self.assertEqual(entries, [(h, RADIX, BLOCK, param)])


class TestRetryAll(unittest.TestCase):
    def test_a_redirect_retry_keeps_the_value_shape(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        from astreum.communication.models.relay_endpoint import RelayEndpoint
        from astreum.communication.storage_response.retry import (
            _retry_pending_storage_get_via_peer_contact,
        )
        from astreum.storage.requests import claim_expr_req

        provider = X25519PrivateKey.generate()
        relay = provider.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        node = _node()
        h = _ids(1)[0]
        param = encode_radix_all_param(VALUE_SHAPE)
        claim_expr_req(node, h, RADIX_ALL, BLOCK, param)
        self.assertTrue(
            _retry_pending_storage_get_via_peer_contact(
                node, expr_id=h, peer_contact=RelayEndpoint(relay, ("10.1.1.1", 6000))
            )
        )
        shared = provider.exchange(node.relay_secret_key.public_key())
        flush_storage_requests(node)
        (_, entries), = _drain(node, shared)
        self.assertEqual(entries, [(h, RADIX_ALL, BLOCK, param)])


class TestServe(unittest.TestCase):
    def setUp(self):
        self.items = [bytes_(b"a"), bytes_(b"b")]
        self.lst = exprs_to_linked_expr(self.items)
        self.store = make_store(self.lst)
        self.helper = TestGetServer("test_mixed_found_redirect_unknown")
        self.helper.setUp()

    def serve(self, param, walked, node=None, resolution=RADIX, target="walk_radix_key"):
        entries = [(self.lst.hash(), resolution, None, param)]
        walk_result = (walked, []) if target == "collect_radix_all" else walked
        with patch.object(req_handle, target, return_value=walk_result) as walk:
            result, responses, _ = self.helper._serve(entries, local=self.store, node=node)
        return result, responses, walk

    def _sent(self, responses):
        sent = []
        for response in responses:
            self.assertEqual(response.code, StorageResponseCode.STORAGE_FOUND)
            sent.extend(decode_found_page(response.data)[2])
        return sent

    def test_serves_what_the_walk_returns(self):
        walked = list(self.store.values())
        result, responses, walk = self.serve(encode_radix_key_param(8, KEY, VALUE_SHAPE), walked)
        self.assertEqual(result, (True, None))
        self.assertEqual({e.hash() for e in self._sent(responses)}, {e.hash() for e in walked})
        args = walk.call_args.args
        self.assertEqual(args[1:], (self.lst.hash(), KEY, 8, VALUE_SHAPE))

    def test_a_walk_that_selects_nothing_is_refused(self):
        result, responses, _ = self.serve(encode_radix_key_param(0, KEY), [])
        self.assertEqual(result, (False, "radix selects nothing"))
        self.assertEqual(responses, [])

    def test_a_key_longer_than_the_limit_is_refused(self):
        node = self.helper._node()
        node.config.storage_radix_key_max_bytes = 8
        result, responses, walk = self.serve(encode_radix_key_param(0, KEY), [], node=node)
        self.assertEqual(result, (False, "radix key too long"))
        self.assertEqual(responses, [])
        walk.assert_not_called()

    def test_a_value_shape_longer_than_the_limit_is_refused(self):
        node = self.helper._node()
        node.config.storage_shape_max_bytes = 1
        for resolution, param, target in (
            (RADIX, encode_radix_key_param(0, KEY, VALUE_SHAPE), "walk_radix_key"),
            (RADIX_ALL, encode_radix_all_param(VALUE_SHAPE), "collect_radix_all"),
        ):
            with self.subTest(resolution=resolution):
                result, responses, walk = self.serve(
                    param, [], node=node, resolution=resolution, target=target
                )
                self.assertEqual(result, (False, "value shape too long"))
                self.assertEqual(responses, [])
                walk.assert_not_called()

    def test_serves_all_with_the_value_shape_and_a_page_budget(self):
        walked = list(self.store.values())
        result, responses, walk = self.serve(
            encode_radix_all_param(VALUE_SHAPE), walked,
            resolution=RADIX_ALL, target="collect_radix_all",
        )
        self.assertEqual(result, (True, None))
        self.assertEqual({e.hash() for e in self._sent(responses)}, {e.hash() for e in walked})
        args = walk.call_args.args
        self.assertEqual(args[1:], (self.lst.hash(), None, VALUE_SHAPE))
        node = self.helper._node()
        self.assertEqual(
            walk.call_args.kwargs,
            {
                "follow_refs": False,
                "budget": (node.config.storage_found_max_pages - 1) * PAGE_ENTRY_BUDGET,
            },
        )

    def test_an_all_walk_that_selects_nothing_is_refused(self):
        result, responses, _ = self.serve(
            encode_radix_all_param(), [], resolution=RADIX_ALL, target="collect_radix_all"
        )
        self.assertEqual(result, (False, "radix selects nothing"))
        self.assertEqual(responses, [])


class TestCull(unittest.TestCase):
    def test_keeps_everything_delivered_root_first(self):
        items = [bytes_(b"a"), bytes_(b"b"), bytes_(b"c")]
        lst = exprs_to_linked_expr(items)
        everything = list(make_store(lst).values())
        kept = cull_to_resolution(lst, everything, RADIX, encode_radix_key_param(0, KEY))
        self.assertEqual(kept[0].hash(), lst.hash())
        self.assertEqual({e.hash() for e in kept}, {e.hash() for e in everything})
        self.assertEqual(len(kept), len(everything))

    def test_keeps_everything_delivered_for_all(self):
        items = [bytes_(b"a"), bytes_(b"b"), bytes_(b"c")]
        lst = exprs_to_linked_expr(items)
        everything = list(make_store(lst).values())
        kept = cull_to_resolution(lst, everything, RADIX_ALL, encode_radix_all_param())
        self.assertEqual(kept[0].hash(), lst.hash())
        self.assertEqual({e.hash() for e in kept}, {e.hash() for e in everything})
        self.assertEqual(len(kept), len(everything))


if __name__ == "__main__":
    unittest.main()
