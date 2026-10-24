"""``walk_radix``, the RADIX request on the client, and the ``local`` arg."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.storage_request.model import parse_radix_param  # noqa: E402
from astreum.expression import RESOLUTION_RADIX, bytes_  # noqa: E402
from astreum.expression.encoding import (  # noqa: E402
    decode_expr_from_bytes,
    encode_expr_to_bytes,
)
from astreum.storage.exprs import get_expr, get_radix_from_network  # noqa: E402
from astreum.storage.radix import (  # noqa: E402
    RadixTree,
    get_all_from_radix_tree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.tree.walk import walk_radix  # noqa: E402
from tests.unit.storage.test_contact import QUEUE, _put, _store_node  # noqa: E402


def _build(source, count: int = 12):
    """Put a tree of *count* keys in *source*. Return root hash and values."""
    built = RadixTree()
    values = {bytes([i * 7 % 256]) * 4: bytes_(bytes([i]) * 8) for i in range(1, count + 1)}
    for key, value in values.items():
        put_in_radix_tree(built, source, key, value)
    for node in built.nodes.values():
        _put(source, get_radix_node_expr(node))
        if node.value is not None and not isinstance(node.value, bytes):
            _put(source, node.value)
    return built.root_hash, values


class _Provider:
    """Patch ``queue_storage_get`` so *source* answers, as a server would."""

    def __init__(self, source, requester, answer_radix: bool = True):
        self.source = source
        self.requester = requester
        self.answer_radix = answer_radix
        self.calls: list[tuple[bytes, int]] = []

    def __enter__(self):
        def fake_queue(node, expr_id, resolution, *, contact=None, preclaimed=False,
                       block_hash=None, shape=b""):
            self.calls.append((expr_id, resolution))
            if resolution == RESOLUTION_RADIX:
                if not self.answer_radix:
                    return False
                offset, key = parse_radix_param(shape)
                served = walk_radix(self.source, expr_id, key, offset)
            else:
                expr = self.source.hot_storage.get(expr_id)
                served = [] if expr is None else [expr]
            if not served:
                return False
            for item in served:
                self.requester.hot_storage[item.hash()] = decode_expr_from_bytes(
                    encode_expr_to_bytes(item)
                )
            return True

        self._patch = patch(QUEUE, side_effect=fake_queue)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    @property
    def radix_calls(self) -> int:
        return sum(1 for _, r in self.calls if r == RESOLUTION_RADIX)


class TestWalkRadix(unittest.TestCase):
    def setUp(self):
        self.source = _store_node()
        self.root, self.values = _build(self.source)
        self.key = next(iter(self.values))

    def _fresh_with(self, exprs):
        store = _store_node()
        store.is_connected = False
        for expr in exprs:
            store.hot_storage[expr.hash()] = decode_expr_from_bytes(encode_expr_to_bytes(expr))
        return store

    def test_root_comes_first(self):
        exprs = walk_radix(self.source, self.root, self.key, 0)
        self.assertEqual(exprs[0].hash(), self.root)

    def test_each_expr_is_sent_once(self):
        exprs = walk_radix(self.source, self.root, self.key, 0)
        hashes = [e.hash() for e in exprs]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_the_key_is_found_from_the_exprs_alone(self):
        for key, value in self.values.items():
            with self.subTest(key=key):
                store = self._fresh_with(walk_radix(self.source, self.root, key, 0))
                got = get_from_radix_tree(RadixTree(root_hash=self.root), store, key, local=True)
                self.assertEqual(got.hash(), value.hash())

    def test_the_walk_sends_less_than_the_whole_tree(self):
        exprs = walk_radix(self.source, self.root, self.key, 0)
        self.assertLess(len(exprs), len(self.source.hot_storage))

    def test_a_key_that_is_not_in_the_tree_sends_the_nodes_it_passed(self):
        exprs = walk_radix(self.source, self.root, b"\xfe" * 4, 0)
        self.assertTrue(exprs)
        self.assertEqual(exprs[0].hash(), self.root)

    def test_nothing_is_sent_if_the_root_is_not_held(self):
        self.assertEqual(walk_radix(self.source, b"\x09" * 32, self.key, 0), [])

    def test_an_offset_outside_the_key_sends_nothing(self):
        self.assertEqual(walk_radix(self.source, self.root, self.key, 33), [])

    def test_the_walk_stops_at_a_node_that_is_not_held(self):
        tree = RadixTree(root_hash=self.root)
        get_from_radix_tree(tree, self.source, self.key, local=True)
        path = list(tree.nodes)
        self.assertGreaterEqual(len(path), 3)
        full = [e.hash() for e in walk_radix(self.source, self.root, self.key, 0)]
        broken = _store_node()
        broken.hot_storage.update(self.source.hot_storage)
        del broken.hot_storage[path[1]]
        cut = [e.hash() for e in walk_radix(broken, self.root, self.key, 0)]
        self.assertEqual(cut[0], self.root)
        self.assertLess(len(cut), len(full))
        self.assertNotIn(path[2], cut)

    def test_a_walk_from_an_inner_node_matches_the_tail_of_a_full_walk(self):
        tree = RadixTree(root_hash=self.root)
        get_from_radix_tree(tree, self.source, self.key, local=True)
        path = list(tree.nodes)
        inner = tree.nodes[path[0]]
        offset = inner.key_len + 1
        from_inner = walk_radix(self.source, path[1], self.key, offset)
        full = {e.hash() for e in walk_radix(self.source, self.root, self.key, 0)}
        self.assertEqual(from_inner[0].hash(), path[1])
        self.assertTrue({e.hash() for e in from_inner} <= full)

    def test_the_walk_does_not_use_the_network(self):
        self.source.is_connected = True
        with patch(QUEUE) as queue:
            walk_radix(self.source, self.root, self.key, 0)
        queue.assert_not_called()


class TestLocalArg(unittest.TestCase):
    def setUp(self):
        self.source = _store_node()
        self.root, self.values = _build(self.source)
        self.key = next(iter(self.values))
        self.requester = _store_node()

    def test_get_expr_local_skips_the_network(self):
        with _Provider(self.source, self.requester) as provider:
            self.assertIsNone(get_expr(self.requester, self.root, local=True))
        self.assertEqual(provider.calls, [])

    def test_get_expr_local_still_reads_local_storage(self):
        expr = bytes_(b"here")
        self.requester.hot_storage[expr.hash()] = expr
        self.assertEqual(get_expr(self.requester, expr.hash(), local=True).hash(), expr.hash())

    def test_a_local_lookup_on_an_empty_store_finds_nothing_and_sends_nothing(self):
        with _Provider(self.source, self.requester) as provider:
            got = get_from_radix_tree(
                RadixTree(root_hash=self.root), self.requester, self.key, local=True
            )
        self.assertIsNone(got)
        self.assertEqual(provider.calls, [])

    def test_the_default_still_uses_the_network(self):
        with _Provider(self.source, self.requester) as provider:
            got = get_from_radix_tree(RadixTree(root_hash=self.root), self.requester, self.key)
        self.assertEqual(got.hash(), self.values[self.key].hash())
        self.assertTrue(provider.calls)


class TestClient(unittest.TestCase):
    def setUp(self):
        self.source = _store_node()
        self.root, self.values = _build(self.source)
        self.key = next(iter(self.values))
        self.requester = _store_node()

    def _lookup(self, key=None):
        key = key or self.key
        return get_from_radix_tree(RadixTree(root_hash=self.root), self.requester, key)

    def test_a_lookup_sends_one_radix_request(self):
        with _Provider(self.source, self.requester) as provider:
            got = self._lookup()
        self.assertEqual(got.hash(), self.values[self.key].hash())
        self.assertEqual(provider.radix_calls, 1)

    def test_a_lookup_sends_fewer_requests_than_without_radix(self):
        with _Provider(self.source, self.requester, answer_radix=False) as without:
            self._lookup()
        self.requester.hot_storage.clear()
        with _Provider(self.source, self.requester) as with_radix:
            self._lookup()
        self.assertLess(len(with_radix.calls), len(without.calls))

    def test_each_key_of_a_cold_store_costs_at_most_one_radix_request(self):
        source = _store_node()
        root, values = _build(source, 60)
        requester = _store_node()
        with _Provider(source, requester) as provider:
            for key, value in values.items():
                before = len(provider.calls)
                got = get_from_radix_tree(RadixTree(root_hash=root), requester, key)
                self.assertEqual(got.hash(), value.hash())
                sent = provider.calls[before:]
                self.assertLessEqual(len(sent), 1, msg=key.hex())
                self.assertTrue(all(r == RESOLUTION_RADIX for _, r in sent))

    def test_a_node_whose_root_is_held_but_cells_are_not_is_requested(self):
        root_expr = self.source.hot_storage[self.root]
        self.requester.hot_storage[self.root] = decode_expr_from_bytes(
            encode_expr_to_bytes(root_expr)
        )
        with _Provider(self.source, self.requester) as provider:
            got = self._lookup()
        self.assertEqual(got.hash(), self.values[self.key].hash())
        self.assertEqual(provider.radix_calls, 1)
        self.assertEqual(len(provider.calls), 1)

    def test_a_second_lookup_of_the_same_key_sends_nothing(self):
        with _Provider(self.source, self.requester) as provider:
            self._lookup()
            before = len(provider.calls)
            self._lookup()
        self.assertEqual(len(provider.calls), before)

    def test_every_key_is_found(self):
        with _Provider(self.source, self.requester):
            for key, value in self.values.items():
                with self.subTest(key=key):
                    self.assertEqual(self._lookup(key).hash(), value.hash())

    def test_a_lookup_falls_back_when_the_radix_request_is_not_answered(self):
        with _Provider(self.source, self.requester, answer_radix=False) as provider:
            got = self._lookup()
        self.assertEqual(got.hash(), self.values[self.key].hash())
        self.assertGreaterEqual(provider.radix_calls, 1)

    def test_a_full_walk_sends_no_radix_request(self):
        with _Provider(self.source, self.requester) as provider:
            everything = get_all_from_radix_tree(RadixTree(root_hash=self.root), self.requester)
        self.assertEqual(set(everything), set(self.values))
        self.assertEqual(provider.radix_calls, 0)

    def test_a_key_longer_than_the_limit_is_refused(self):
        self.requester.config.storage_radix_key_max_bytes = 2
        with self.assertRaises(ValueError):
            get_radix_from_network(self.requester, self.root, b"\x01" * 3, 0)

    def test_a_node_that_is_held_is_not_requested(self):
        with _Provider(self.source, self.requester) as provider:
            self.requester.hot_storage.update(self.source.hot_storage)
            get_radix_from_network(self.requester, self.root, self.key, 0)
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
