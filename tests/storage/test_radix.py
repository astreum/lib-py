from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import Expr, NIL, ZERO32, int_, bytes_, link
from astreum.node import Node
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.storage.radix.node import (
    RadixNode,
    radix_node_hash,
    convert_radix_node_to_expr,
    get_radix_node_from_storage,
)
from astreum.storage.radix.tree.model import RadixTree
from astreum.storage.radix.tree.put import put_in_radix_tree
from astreum.storage.radix.tree.all import get_all_from_radix_tree
from astreum.consensus.models.accounts import _trie_nodes_exprs


class TestRadixNodeEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = Node(
            {
                "cold_storage_path": self.temp_dir.name,
                "cold_storage_scale": "KB",
                "cold_storage_base_size": 10 * 1024 * 1024,
                "default_seed": None,
                "verbose": False,
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _store_radix_node_and_deps(self, node: RadixNode) -> bytes:
        expr = convert_radix_node_to_expr(node)
        h = expr.hash()
        stored = put_expr_in_cold_storage(self.node, expr)
        self.assertTrue(stored, "failed to store radix node expr")
        return h

    def test_leaf_node_with_expr_value(self) -> None:
        rn = RadixNode(
            key_len=8,
            key=b"\x00",
            value=int_(42),
            child_0=None,
            child_1=None,
        )
        h = self._store_radix_node_and_deps(rn)
        loaded = get_radix_node_from_storage(self.node, h)
        self.assertEqual(loaded.key_len, 8)
        self.assertEqual(loaded.key, b"\x00")
        self.assertIsNotNone(loaded.value)
        self.assertEqual(loaded.value.hash(), int_(42).hash())
        self.assertIsNone(loaded.child_0)
        self.assertIsNone(loaded.child_1)

    def test_leaf_node_with_bytes_value(self) -> None:
        value_expr = bytes_(b"stored_value")
        real_hash = value_expr.hash()
        put_expr_in_cold_storage(self.node, value_expr)
        rn = RadixNode(
            key_len=16,
            key=b"\x01\x02",
            value=real_hash,
            child_0=None,
            child_1=None,
        )
        h = self._store_radix_node_and_deps(rn)
        loaded = get_radix_node_from_storage(self.node, h)
        self.assertEqual(loaded.key_len, 16)
        self.assertEqual(loaded.key, b"\x01\x02")
        self.assertIsNotNone(loaded.value)
        self.assertEqual(loaded.value.hash(), real_hash)
        self.assertIsNone(loaded.child_0)
        self.assertIsNone(loaded.child_1)

    def test_leaf_node_with_nil_value(self) -> None:
        rn = RadixNode(
            key_len=0,
            key=b"",
            value=None,
            child_0=None,
            child_1=None,
        )
        h = self._store_radix_node_and_deps(rn)
        loaded = get_radix_node_from_storage(self.node, h)
        self.assertEqual(loaded.key_len, 0)
        self.assertEqual(loaded.key, b"")
        self.assertIsNone(loaded.value)
        self.assertIsNone(loaded.child_0)
        self.assertIsNone(loaded.child_1)

    def test_internal_node_with_both_children(self) -> None:
        leaf_value = int_(999)
        leaf = RadixNode(
            key_len=24,
            key=b"\xab\xcd\xef",
            value=leaf_value,
            child_0=None,
            child_1=None,
        )
        leaf_hash = radix_node_hash(leaf)
        self._store_radix_node_and_deps(leaf)

        other = RadixNode(
            key_len=8,
            key=b"\xaa",
            value=int_(1),
            child_0=None,
            child_1=None,
        )
        child_1_hash = radix_node_hash(other)
        self._store_radix_node_and_deps(other)

        internal = RadixNode(
            key_len=8,
            key=b"\xff",
            value=None,
            child_0=leaf_hash,
            child_1=child_1_hash,
        )
        h = self._store_radix_node_and_deps(internal)
        loaded = get_radix_node_from_storage(self.node, h)
        self.assertEqual(loaded.key_len, 8)
        self.assertEqual(loaded.key, b"\xff")
        self.assertIsNone(loaded.value)
        self.assertEqual(loaded.child_0, leaf_hash)
        self.assertEqual(loaded.child_1, child_1_hash)

    def test_hash_stability(self) -> None:
        rn = RadixNode(
            key_len=16,
            key=b"\xaa\xbb",
            value=int_(7),
            child_0=ZERO32,
            child_1=None,
        )
        h1 = radix_node_hash(rn)
        h2 = radix_node_hash(rn)
        self.assertEqual(h1, h2)

    def test_deeply_nested_node(self) -> None:
        bottom = RadixNode(
            key_len=8, key=b"\x01", value=int_(1), child_0=None, child_1=None
        )
        ch0_h = radix_node_hash(bottom)
        self._store_radix_node_and_deps(bottom)

        middle = RadixNode(
            key_len=8, key=b"\x02", value=None, child_0=ch0_h, child_1=None
        )
        ch1_h = radix_node_hash(middle)
        self._store_radix_node_and_deps(middle)

        top = RadixNode(
            key_len=8, key=b"\x03", value=int_(3), child_0=ch0_h, child_1=ch1_h
        )
        top_h = self._store_radix_node_and_deps(top)

        loaded = get_radix_node_from_storage(self.node, top_h)
        self.assertEqual(loaded.key_len, 8)
        self.assertEqual(loaded.key, b"\x03")
        self.assertIsNotNone(loaded.value)
        self.assertEqual(loaded.value.hash(), int_(3).hash())
        self.assertEqual(loaded.child_0, ch0_h)
        self.assertEqual(loaded.child_1, ch1_h)


class TestRadixTreeGetAll(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = Node(config={
            "cold_storage_path": self.temp_dir.name,
            "cold_storage_scale": "KB",
            "default_seed": None,
            "verbose": False,
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def _store_tree(self, tree: RadixTree):
        for e in _trie_nodes_exprs(tree):
            put_expr_in_cold_storage(self.node, e)

    def test_empty_tree(self):
        tree = RadixTree()
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(result, {})

    def test_single_entry(self):
        tree = RadixTree()
        val = int_(42)
        put_in_radix_tree(tree, self.node, b"\x01", val)
        self._store_tree(tree)
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x01"].hash(), val.hash())

    def test_single_entry_from_root_hash(self):
        tree = RadixTree()
        val = int_(42)
        put_in_radix_tree(tree, self.node, b"\x01", val)
        self._store_tree(tree)
        fresh = RadixTree(root_hash=tree.root_hash)
        result = get_all_from_radix_tree(fresh, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x01"].hash(), val.hash())

    def test_multiple_entries_no_shared_prefix(self):
        tree = RadixTree()
        vals = {b"\x00": int_(1), b"\x80": int_(2), b"\x40": int_(3)}
        for k, v in vals.items():
            put_in_radix_tree(tree, self.node, k, v)
        self._store_tree(tree)
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(result), 3)
        for k, v in vals.items():
            self.assertEqual(result[k].hash(), v.hash())

    def test_multiple_entries_no_shared_prefix_from_root_hash(self):
        tree = RadixTree()
        vals = {b"\x00": int_(1), b"\x80": int_(2), b"\x40": int_(3)}
        for k, v in vals.items():
            put_in_radix_tree(tree, self.node, k, v)
        self._store_tree(tree)
        fresh = RadixTree(root_hash=tree.root_hash)
        result = get_all_from_radix_tree(fresh, self.node)
        self.assertEqual(len(result), 3)
        for k, v in vals.items():
            self.assertEqual(result[k].hash(), v.hash())

    def test_shared_prefix_branching(self):
        tree = RadixTree()
        vals = {b"\xab\x00": int_(1), b"\xab\x80": int_(2), b"\xab\x40": int_(3)}
        for k, v in vals.items():
            put_in_radix_tree(tree, self.node, k, v)
        self._store_tree(tree)
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(result), 3)
        for k, v in vals.items():
            self.assertEqual(result[k].hash(), v.hash())

    def test_shared_prefix_branching_from_root_hash(self):
        tree = RadixTree()
        vals = {b"\xab\x00": int_(1), b"\xab\x80": int_(2), b"\xab\x40": int_(3)}
        for k, v in vals.items():
            put_in_radix_tree(tree, self.node, k, v)
        self._store_tree(tree)
        fresh = RadixTree(root_hash=tree.root_hash)
        result = get_all_from_radix_tree(fresh, self.node)
        self.assertEqual(len(result), 3)
        for k, v in vals.items():
            self.assertEqual(result[k].hash(), v.hash())

    def test_overwrite_existing_key(self):
        tree = RadixTree()
        val_a = int_(10)
        val_b = int_(20)
        put_in_radix_tree(tree, self.node, b"\x01", val_a)
        put_in_radix_tree(tree, self.node, b"\x01", val_b)
        self._store_tree(tree)
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x01"].hash(), val_b.hash())

    def test_overwrite_from_root_hash(self):
        tree = RadixTree()
        val_a = int_(10)
        val_b = int_(20)
        put_in_radix_tree(tree, self.node, b"\x01", val_a)
        put_in_radix_tree(tree, self.node, b"\x01", val_b)
        self._store_tree(tree)
        fresh = RadixTree(root_hash=tree.root_hash)
        result = get_all_from_radix_tree(fresh, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x01"].hash(), val_b.hash())

    def test_deeply_nested_single_path(self):
        tree = RadixTree()
        val = int_(99)
        put_in_radix_tree(tree, self.node, b"\x00\x00\x00\x00\x00\x00\x00\x00", val)
        self._store_tree(tree)
        result = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x00\x00\x00\x00\x00\x00\x00\x00"].hash(), val.hash())

    def test_deeply_nested_single_path_from_root_hash(self):
        tree = RadixTree()
        val = int_(99)
        put_in_radix_tree(tree, self.node, b"\x00\x00\x00\x00\x00\x00\x00\x00", val)
        self._store_tree(tree)
        fresh = RadixTree(root_hash=tree.root_hash)
        result = get_all_from_radix_tree(fresh, self.node)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[b"\x00\x00\x00\x00\x00\x00\x00\x00"].hash(), val.hash())


if __name__ == "__main__":
    unittest.main()
