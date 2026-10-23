"""get_expr_by_shape: one shaped request, then SINGLE requests for the gaps."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
for path in (ROOT, SRC_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.consensus.block.encoding.shape import BLOCK_SHAPE  # noqa: E402
from astreum.expression import (  # noqa: E402
    RESOLUTION_DYNAMIC,
    RESOLUTION_SINGLE,
    bytes_,
    exprs_to_linked_expr,
)
from astreum.expression.shape import parse_shape, repeat, SEND, walk_shape  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.exprs import get_expr_by_shape, put_expr_in_hot_storage  # noqa: E402
from tests.unit.consensus.block.test_expr import _make_block  # noqa: E402
from tests.unit.expression.test_shape import make_store  # noqa: E402

QUEUE = "astreum.storage.workers.requests.queue_storage_get"
NETWORK = "astreum.storage.exprs.network.get_exprs_from_network"


def _node(connected: bool = True) -> Node:
    node = Node(config={"default_seed": None, "verbose": False})
    node.is_connected = connected
    node.config.storage_fetch_interval = 0
    node.config.storage_fetch_retries = 2
    return node


class FakeProvider:
    """Answers a DYNAMIC request by walking its store into the client's hot storage."""

    def __init__(self, node, store, drop=()):
        self.node, self.store, self.drop = node, store, set(drop)
        self.requests = []

    def queue(self, node, expr_id, resolution, **kwargs):
        self.requests.append((expr_id, resolution, kwargs.get("shape", b"")))
        if resolution == RESOLUTION_DYNAMIC:
            found, _ = walk_shape(
                parse_shape(kwargs["shape"]),
                expr_id,
                lambda h: None if h in self.drop else self.store.get(h),
            )
            for expr in reversed(found):
                put_expr_in_hot_storage(self.node, expr)
        return True


class TestGetByShape(unittest.TestCase):
    def setUp(self):
        self.items = [bytes_(b"a"), bytes_(b"b"), bytes_(b"c")]
        self.lst = exprs_to_linked_expr(self.items)
        self.store = make_store(self.lst)
        self.node = _node()

    def test_one_shaped_request_brings_everything(self):
        provider = FakeProvider(self.node, self.store)
        shape = repeat(SEND)
        with patch(QUEUE, side_effect=provider.queue), patch(NETWORK) as network:
            root = get_expr_by_shape(self.node, self.lst.hash(), shape)
        self.assertEqual(root.hash(), self.lst.hash())
        self.assertEqual(provider.requests, [(self.lst.hash(), RESOLUTION_DYNAMIC, shape)])
        network.assert_not_called()

    def test_a_missing_child_is_fetched_as_a_single_request(self):
        dropped = self.items[1].hash()
        provider = FakeProvider(self.node, self.store, drop={dropped})
        with patch(QUEUE, side_effect=provider.queue), patch(NETWORK) as network:
            get_expr_by_shape(self.node, self.lst.hash(), repeat(SEND))
        network.assert_called_once()
        self.assertEqual(network.call_args.args[1], [(dropped, RESOLUTION_SINGLE)])

    def test_nothing_is_requested_when_it_is_all_local(self):
        for expr in self.store.values():
            put_expr_in_hot_storage(self.node, expr)
        with patch(QUEUE) as queue, patch(NETWORK) as network:
            root = get_expr_by_shape(self.node, self.lst.hash(), repeat(SEND))
        self.assertIsNotNone(root)
        queue.assert_not_called()
        network.assert_not_called()

    def test_returns_none_when_the_root_never_arrives(self):
        with patch(QUEUE, return_value=True), patch(
            "astreum.storage.exprs.network._resend_to_next_provider", return_value=set()
        ) as resend:
            self.assertIsNone(get_expr_by_shape(self.node, self.lst.hash(), SEND))
        # The second provider is asked once, with the same shape.
        self.assertEqual(resend.call_args.args[1], [(self.lst.hash(), RESOLUTION_DYNAMIC)])
        self.assertEqual(resend.call_args.args[3], SEND)

    def test_a_node_that_is_not_connected_sends_nothing(self):
        node = _node(connected=False)
        with patch(QUEUE) as queue:
            self.assertIsNone(get_expr_by_shape(node, self.lst.hash(), SEND))
        queue.assert_not_called()

    def test_rejects_a_bad_or_long_shape(self):
        with self.assertRaises(ValueError):
            get_expr_by_shape(self.node, self.lst.hash(), b"\x09")
        self.node.config.storage_shape_max_bytes = 2
        with self.assertRaises(ValueError):
            get_expr_by_shape(self.node, self.lst.hash(), repeat(repeat(SEND)))


class TestBlockShape(unittest.TestCase):
    def setUp(self):
        self.block = _make_block(statistics=[(1, 1, 0, 0), (2, 3, 4, 5)])
        self.expr = get_block_expr(self.block)
        self.store = make_store(self.expr)

    def test_shape_is_small(self):
        self.assertLessEqual(len(BLOCK_SHAPE), 128)
        parse_shape(BLOCK_SHAPE)

    def test_shape_reaches_everything_the_decoder_reads_and_nothing_more(self):
        found, missing = walk_shape(
            parse_shape(BLOCK_SHAPE), self.expr.hash(), self.store.get
        )
        self.assertEqual(missing, [])
        # Everything below the root is stored here, so the shape leaves some out.
        self.assertLess(len(found), len(self.store) + 1)
        reached = {e.hash() for e in found}
        self.assertEqual(found[0].hash(), self.expr.hash())
        self.assertEqual(len(reached), len(found))

    def test_a_node_holding_the_walk_decodes_with_no_network_use(self):
        found, _ = walk_shape(parse_shape(BLOCK_SHAPE), self.expr.hash(), self.store.get)
        node = _node(connected=False)
        for expr in reversed(found):
            put_expr_in_hot_storage(node, expr)
        with patch("astreum.storage.exprs.cascade.get_expr_from_network") as network:
            decoded = get_block_from_storage(node, self.expr.hash(), load_bloom=False)
        network.assert_not_called()
        self.assertEqual(decoded.height, self.block.height)
        self.assertEqual(decoded.statistics, [(1, 1, 0, 0), (2, 3, 4, 5)])
        self.assertEqual(decoded.timestamp, self.block.timestamp)

    def test_decode_makes_one_shaped_request(self):
        provider = FakeProvider(_node(), self.store)
        node = provider.node
        with patch(QUEUE, side_effect=provider.queue), patch(
            "astreum.storage.exprs.cascade.get_expr_from_network"
        ) as network:
            decoded = get_block_from_storage(node, self.expr.hash(), load_bloom=False)
        self.assertEqual(
            provider.requests, [(self.expr.hash(), RESOLUTION_DYNAMIC, BLOCK_SHAPE)]
        )
        network.assert_not_called()
        self.assertEqual(decoded.height, self.block.height)


if __name__ == "__main__":
    unittest.main()
