"""``collect_radix_all``, the RADIX_ALL request on the client, and value shapes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.storage_request.model import (  # noqa: E402
    parse_radix_all_param,
    parse_radix_key_param,
)
from astreum.expression import (  # noqa: E402
    RESOLUTION_RADIX_ALL,
    RESOLUTION_RADIX_KEY,
    bytes_,
    exprs_to_linked_expr,
    resolve_inner_exprs,
)
from astreum.expression.encoding import (  # noqa: E402
    decode_expr_from_bytes,
    encode_expr_to_bytes,
)
from astreum.expression.shape import SEND, repeat  # noqa: E402
from astreum.storage.exprs import get_radix_all_from_network  # noqa: E402
from astreum.storage.radix import (  # noqa: E402
    RadixTree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.tree.walk import collect_radix_all, walk_radix_key  # noqa: E402
from tests.unit.storage.test_contact import QUEUE, _put, _store_node  # noqa: E402

B1 = b"\x01" * 32
B2 = b"\x02" * 32
B3 = b"\x03" * 32

# One level below the cells of a value: the items of the inner lists.
DEEP = repeat(repeat(SEND))


def _value(i: int):
    """A value of two inner lists, so a value shape can reach below the items."""
    inner = lambda tag: exprs_to_linked_expr([bytes_(bytes([i, tag, 1])), bytes_(bytes([i, tag, 2]))])
    return exprs_to_linked_expr([inner(1), inner(2)])


def _inner_items(value) -> set[bytes]:
    """The hashes of the bytes exprs at the bottom of a value."""
    return {e.hash() for e in resolve_inner_exprs(None, value)[0] if e.base != "link"} or set()


def _write_block(source, parent, parent_block, keys):
    """Write *keys* on top of the tree *parent* and store the cells of its nodes.

    Returns the new tree and the set of node hashes that are new.
    """
    tree = RadixTree(root_hash=parent, root_block=parent_block)
    before = set(tree.nodes)
    values = {}
    for i in keys:
        key = bytes([i * 7 % 256]) * 4
        values[key] = _value(i)
        put_in_radix_tree(tree, source, key, values[key])
    new = set()
    for node_hash, node in tree.nodes.items():
        expr = get_radix_node_expr(node)
        if expr.hash() != node_hash:
            continue
        if node_hash not in source.hot_storage:
            new.add(node_hash)
        for item in resolve_inner_exprs(source, expr)[0]:
            _put(source, item)
        if node.value is not None and not isinstance(node.value, bytes):
            for item in resolve_inner_exprs(source, node.value)[0]:
                _put(source, item)
    return tree, new, values


class _Chain:
    """Three blocks on one source: the later blocks point to nodes of earlier ones."""

    def __init__(self):
        self.source = _store_node()
        self.tree1, self.new1, v1 = _write_block(self.source, None, None, range(1, 25))
        self.tree2, self.new2, v2 = _write_block(
            self.source, self.tree1.root_hash, B1, range(30, 34)
        )
        self.tree3, self.new3, v3 = _write_block(
            self.source, self.tree2.root_hash, B2, range(40, 42)
        )
        self.values = {**v1, **v2, **v3}
        self.root1, self.root2, self.root3 = (
            self.tree1.root_hash,
            self.tree2.root_hash,
            self.tree3.root_hash,
        )


_CHAIN = None


def _chain() -> _Chain:
    """One chain for all tests. No test writes to its store."""
    global _CHAIN
    if _CHAIN is None:
        _CHAIN = _Chain()
    return _CHAIN


def _hashes(exprs) -> set[bytes]:
    return {e.hash() for e in exprs}


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.chain = _chain()
        self.source = self.chain.source

    def test_the_start_node_comes_first(self):
        exprs, _ = collect_radix_all(self.source, self.chain.root3, B3)
        self.assertEqual(exprs[0].hash(), self.chain.root3)

    def test_each_expr_is_collected_once(self):
        exprs, _ = collect_radix_all(self.source, self.chain.root3, B3, DEEP)
        hashes = [e.hash() for e in exprs]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_following_refs_reads_nodes_of_older_blocks(self):
        exprs, missing = collect_radix_all(self.source, self.chain.root3, B3)
        self.assertEqual(missing, [])
        got = _hashes(exprs)
        self.assertTrue(self.chain.new3 <= got)
        self.assertTrue(got & self.chain.new2)
        self.assertTrue(got & self.chain.new1)

    def test_without_refs_the_walk_stays_in_the_block(self):
        every, _ = collect_radix_all(self.source, self.chain.root3, B3)
        exprs, missing = collect_radix_all(
            self.source, self.chain.root3, None, follow_refs=False
        )
        self.assertEqual(missing, [])
        got = _hashes(exprs)
        self.assertTrue(self.chain.new3 <= got)
        older = (self.chain.new1 | self.chain.new2) - self.chain.new3
        self.assertTrue(older & _hashes(every))
        self.assertFalse(older & got)

    def test_the_client_walk_finds_the_values_from_the_exprs_alone(self):
        exprs, _ = collect_radix_all(self.source, self.chain.root3, B3, SEND)
        store = _store_node()
        store.is_connected = False
        for expr in exprs:
            store.hot_storage[expr.hash()] = decode_expr_from_bytes(encode_expr_to_bytes(expr))
        tree = RadixTree(root_hash=self.chain.root3, root_block=B3)
        for key, value in self.chain.values.items():
            self.assertEqual(get_from_radix_tree(tree, store, key, local=True).hash(), value.hash())

    def test_a_node_that_is_not_held_is_listed_with_its_block(self):
        tree = RadixTree(root_hash=self.chain.root3, root_block=B3)
        key = next(iter(self.chain.values))
        get_from_radix_tree(tree, self.source, key, local=True)
        path = list(tree.nodes)
        self.assertGreaterEqual(len(path), 3)
        broken = _store_node()
        broken.hot_storage.update(self.source.hot_storage)
        del broken.hot_storage[path[1]]
        exprs, missing = collect_radix_all(broken, self.chain.root3, B3)
        self.assertEqual([h for h, _ in missing], [path[1]])
        self.assertNotIn(path[2], _hashes(exprs))
        # The other branches are still read.
        full, _ = collect_radix_all(self.source, self.chain.root3, B3)
        self.assertGreater(len(exprs), 1)
        self.assertLess(len(exprs), len(full))

    def test_the_block_of_a_listed_node_is_the_block_of_its_pointer(self):
        broken = _store_node()
        broken.hot_storage.update(self.source.hot_storage)
        older = sorted(self.chain.new1 - self.chain.new2 - self.chain.new3)
        self.assertTrue(older)
        del broken.hot_storage[older[0]]
        _, missing = collect_radix_all(broken, self.chain.root3, B3)
        self.assertIn((older[0], B1), missing)

    def test_an_empty_value_shape_collects_no_value_exprs(self):
        plain, _ = collect_radix_all(self.source, self.chain.root3, B3)
        deep, _ = collect_radix_all(self.source, self.chain.root3, B3, DEEP)
        self.assertTrue(_hashes(plain) < _hashes(deep))
        items = set()
        for value in self.chain.values.values():
            items |= _inner_items(value)
        self.assertFalse(items & _hashes(plain))
        self.assertTrue(items <= _hashes(deep))

    def test_a_budget_cuts_at_a_node_boundary(self):
        full, _ = collect_radix_all(self.source, self.chain.root3, B3, DEEP)
        size = sum(4 + len(encode_expr_to_bytes(e)) for e in full)
        cut, missing = collect_radix_all(
            self.source, self.chain.root3, B3, DEEP, budget=size // 2
        )
        self.assertEqual(missing, [])
        self.assertEqual(cut[0].hash(), self.chain.root3)
        self.assertLess(len(cut), len(full))
        self.assertLessEqual(sum(4 + len(encode_expr_to_bytes(e)) for e in cut), size // 2)

    def test_the_start_node_is_collected_with_a_budget_of_zero(self):
        cut, _ = collect_radix_all(self.source, self.chain.root3, B3, budget=0)
        self.assertEqual(cut[0].hash(), self.chain.root3)

    def test_a_start_node_that_is_not_held_gives_nothing(self):
        exprs, missing = collect_radix_all(self.source, b"\x09" * 32, B3)
        self.assertEqual(exprs, [])
        self.assertEqual(missing, [(b"\x09" * 32, B3)])

    def test_the_walk_does_not_use_the_network(self):
        with patch(QUEUE) as queue:
            collect_radix_all(self.source, self.chain.root3, B3, DEEP)
        queue.assert_not_called()

    def test_a_bad_value_shape_is_refused(self):
        with self.assertRaises(ValueError):
            collect_radix_all(self.source, self.chain.root3, B3, b"\x09")


class TestKeyValueShape(unittest.TestCase):
    def setUp(self):
        self.chain = _chain()
        self.source = self.chain.source
        self.key, self.value = next(iter(self.chain.values.items()))
        self.items = _inner_items(self.value)

    def test_the_value_at_the_key_is_walked_with_the_shape(self):
        plain = walk_radix_key(self.source, self.chain.root1, self.key, 0)
        deep = walk_radix_key(self.source, self.chain.root1, self.key, 0, DEEP)
        self.assertFalse(self.items & _hashes(plain))
        self.assertTrue(self.items <= _hashes(deep))

    def test_other_values_are_not_walked(self):
        deep = walk_radix_key(self.source, self.chain.root1, self.key, 0, DEEP)
        others = set()
        for key, value in self.chain.values.items():
            if key != self.key:
                others |= _inner_items(value)
        self.assertFalse(others & _hashes(deep))

    def test_a_key_that_is_not_in_the_tree_walks_no_value(self):
        deep = walk_radix_key(self.source, self.chain.root1, b"\xfe" * 4, 0, DEEP)
        every = set()
        for value in self.chain.values.values():
            every |= _inner_items(value)
        self.assertFalse(every & _hashes(deep))


class _Provider:
    """Patch ``queue_storage_get`` so *source* answers RADIX_ALL as the server does."""

    def __init__(self, source, requester, budget=None, answer=True):
        self.source = source
        self.requester = requester
        self.budget = budget
        self.answer = answer
        self.calls: list[tuple[bytes, int, bytes | None]] = []

    def __enter__(self):
        def fake_queue(node, expr_id, resolution, *, contact=None, preclaimed=False,
                       block_hash=None, shape=b""):
            self.calls.append((expr_id, resolution, block_hash))
            if not self.answer:
                return True
            value_shape = parse_radix_all_param(shape)
            exprs, _ = collect_radix_all(
                self.source, expr_id, None, value_shape, follow_refs=False, budget=self.budget
            )
            if not exprs:
                return False
            for item in exprs:
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
    def ids(self) -> list[bytes]:
        return [h for h, _, _ in self.calls]


class TestValueRef(unittest.TestCase):
    def test_a_node_in_the_chain_holds_a_value_by_a_stored_ref(self):
        # Such a node is new in its block and its value is in an older one.
        found = 0
        for tree in (_chain().tree2, _chain().tree3):
            for node in tree.nodes.values():
                if node.value is not None and node.value_block is not None:
                    found += 1
        self.assertGreater(found, 0)

    def test_the_value_of_such_a_node_is_sent_with_the_node(self):
        chain = _chain()
        exprs, _ = collect_radix_all(
            chain.source, chain.root3, None, SEND, follow_refs=False
        )
        store = _store_node()
        for expr in exprs:
            store.hot_storage[expr.hash()] = decode_expr_from_bytes(encode_expr_to_bytes(expr))
        _, missing = collect_radix_all(store, chain.root3, B3, follow_refs=False)
        self.assertEqual(missing, [])


class TestClient(unittest.TestCase):
    def setUp(self):
        self.chain = _chain()
        self.source = self.chain.source
        self.requester = _store_node()

    def _fetch(self, root=None, block=B3, shape=DEEP):
        return get_radix_all_from_network(
            self.requester, root or self.chain.root3, shape, block_hash=block
        )

    def _read_all(self, root=None, block=B3):
        tree = RadixTree(root_hash=root or self.chain.root3, root_block=block)
        for key, value in self.chain.values.items():
            if get_from_radix_tree(tree, self.requester, key, local=True) is None:
                return False
        return True

    def test_a_cold_store_gets_the_whole_tree(self):
        with _Provider(self.source, self.requester):
            self.assertTrue(self._fetch())
        _, missing = collect_radix_all(self.requester, self.chain.root3, B3)
        self.assertEqual(missing, [])
        self.assertTrue(self._read_all())

    def test_every_request_is_a_radix_all_request_scoped_to_a_block(self):
        with _Provider(self.source, self.requester) as provider:
            self._fetch()
        self.assertTrue(provider.calls)
        self.assertTrue(all(r == RESOLUTION_RADIX_ALL for _, r, _ in provider.calls))
        self.assertEqual(provider.calls[0][2:], (B3,))
        self.assertTrue({b for _, _, b in provider.calls} <= {B3, B2, B1})

    def test_a_node_is_requested_once(self):
        with _Provider(self.source, self.requester) as provider:
            self._fetch()
        self.assertEqual(len(provider.ids), len(set(provider.ids)))

    def test_the_requests_are_far_fewer_than_the_nodes(self):
        with _Provider(self.source, self.requester) as provider:
            self._fetch()
        everything = self.chain.new1 | self.chain.new2 | self.chain.new3
        self.assertLess(len(provider.calls), len(everything))

    def test_the_value_shape_brings_the_value_exprs(self):
        with _Provider(self.source, self.requester):
            self._fetch(shape=DEEP)
        items = set()
        for value in self.chain.values.values():
            items |= _inner_items(value)
        self.assertTrue(items <= set(self.requester.hot_storage))

    def test_without_a_value_shape_the_inner_value_exprs_stay_away(self):
        with _Provider(self.source, self.requester):
            self._fetch(shape=b"")
        items = set()
        for value in self.chain.values.values():
            items |= _inner_items(value)
        self.assertFalse(items & set(self.requester.hot_storage))

    def test_a_second_call_sends_nothing(self):
        with _Provider(self.source, self.requester) as provider:
            self._fetch()
            before = len(provider.calls)
            self.assertTrue(self._fetch())
        self.assertEqual(len(provider.calls), before)

    def test_a_new_block_asks_for_its_new_nodes_only(self):
        with _Provider(self.source, self.requester) as provider:
            self._fetch(self.chain.root2, B2)
            before = len(provider.calls)
            self.assertTrue(self._fetch(self.chain.root3, B3))
        self.assertEqual(provider.ids[before:], [self.chain.root3])

    def test_a_cut_reply_is_finished_in_later_rounds(self):
        with _Provider(self.source, self.requester) as whole:
            self._fetch()
        self.requester.hot_storage.clear()
        with _Provider(self.source, self.requester, budget=1) as cut:
            self.assertTrue(self._fetch())
        self.assertTrue(self._read_all())
        self.assertGreater(len(cut.calls), len(whole.calls))
        self.assertEqual(len(cut.ids), len(set(cut.ids)))

    def test_a_round_that_brings_nothing_ends_the_loop(self):
        with _Provider(self.source, self.requester, answer=False) as provider:
            self.assertFalse(self._fetch())
        self.assertEqual(len(provider.calls), 1)

    def test_nothing_is_sent_if_the_node_is_not_connected(self):
        self.requester.is_connected = False
        with _Provider(self.source, self.requester) as provider:
            self.assertFalse(self._fetch())
        self.assertEqual(provider.calls, [])

    def test_nothing_is_sent_if_nowhere_to_send(self):
        with patch(QUEUE, return_value=False) as queue:
            self.assertFalse(self._fetch())
        self.assertEqual(queue.call_count, 1)

    def test_a_contact_is_asked_without_a_block_route(self):
        seen = []

        def fake_queue(node, expr_id, resolution, **kwargs):
            seen.append(kwargs)
            return False

        contact = object()
        with patch(QUEUE, side_effect=fake_queue):
            get_radix_all_from_network(
                self.requester, self.chain.root3, b"", contact=contact, block_hash=B3
            )
        self.assertEqual(seen[0]["contact"], contact)
        self.assertNotIn("block_hash", seen[0])

    def test_a_value_shape_longer_than_the_limit_is_refused(self):
        self.requester.config.storage_shape_max_bytes = 2
        with self.assertRaises(ValueError):
            self._fetch(shape=DEEP)

    def test_the_param_carries_the_value_shape(self):
        seen = []

        def fake_queue(node, expr_id, resolution, *, shape=b"", **kwargs):
            seen.append(parse_radix_all_param(shape))
            return False

        with patch(QUEUE, side_effect=fake_queue):
            self._fetch(shape=DEEP)
        self.assertEqual(seen, [DEEP])


class TestKeyClientValueShape(unittest.TestCase):
    def test_the_key_request_carries_the_value_shape(self):
        from astreum.storage.exprs import get_radix_key_from_network

        chain = _chain()
        requester = _store_node()
        seen = []

        def fake_queue(node, expr_id, resolution, *, shape=b"", **kwargs):
            seen.append((resolution, parse_radix_key_param(shape)))
            return False

        key = next(iter(chain.values))
        with patch(QUEUE, side_effect=fake_queue):
            get_radix_key_from_network(requester, chain.root1, key, 0, value_shape=DEEP)
        self.assertEqual(seen, [(RESOLUTION_RADIX_KEY, (0, key, DEEP))])

    def test_a_long_value_shape_is_refused(self):
        from astreum.storage.exprs import get_radix_key_from_network

        requester = _store_node()
        requester.config.storage_shape_max_bytes = 2
        with self.assertRaises(ValueError):
            get_radix_key_from_network(requester, b"\x01" * 32, b"k", 0, value_shape=DEEP)


if __name__ == "__main__":
    unittest.main()
