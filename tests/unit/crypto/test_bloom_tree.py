"""Tests for BloomTree and BloomNode: insert and search."""
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.crypto.bloom_tree import BloomNode, BloomTree, bloom_search
from astreum.crypto.bloom_search import make_search_variants


class TestBloomTree(unittest.TestCase):

    def test_empty_search(self):
        results = bloom_search(None, os.urandom(160))
        self.assertEqual(results, [])

    def test_single_block_insert_and_search(self):
        tree = BloomTree()
        block_hash = os.urandom(32)
        tx_hash = os.urandom(32)
        sender = os.urandom(32)
        receiver = os.urandom(32)
        variants = make_search_variants(tx_hash, sender, receiver)

        tree.insert(0, variants)
        tree.set_leaf_start_hash(0, block_hash)

        self.assertEqual(bloom_search(tree.root, variants[0]), [block_hash])
        self.assertEqual(bloom_search(tree.root, variants[1]), [block_hash])
        self.assertEqual(bloom_search(tree.root, variants[2]), [block_hash])

    def test_single_block_search_miss(self):
        tree = BloomTree()
        block_hash = os.urandom(32)
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))

        tree.insert(0, variants)
        tree.set_leaf_start_hash(0, block_hash)
        # Absent elements should almost never match the known block hash
        probes = 2000
        hits = sum(
            block_hash in bloom_search(tree.root, os.urandom(160))
            for _ in range(probes)
        )
        self.assertLess(hits / probes, 0.02)

    def test_two_blocks_different_paths(self):
        tree = BloomTree()
        h1 = os.urandom(32)
        h2 = os.urandom(32)
        v1 = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))
        v2 = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))

        tree.insert(0, v1)
        tree.set_leaf_start_hash(0, h1)
        tree.insert(512, v2)
        tree.set_leaf_start_hash(512, h2)

        self.assertIn(h1, bloom_search(tree.root, v1[0]))
        self.assertIn(h2, bloom_search(tree.root, v2[0]))

    def test_leaf_returns_exact_block_hash(self):
        tree = BloomTree()
        block_hash = os.urandom(32)
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))

        tree.insert(42, variants)
        tree.set_leaf_start_hash(42, block_hash)
        self.assertEqual(bloom_search(tree.root, variants[0]), [block_hash])

    def test_node_levels(self):
        """Root is level 0, children are level+1, leaf is level 10."""
        tree = BloomTree()
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))

        tree.insert(0, variants)

        root = tree.root
        self.assertEqual(root.level, 0)
        self.assertEqual(root.width, 1024)
        self.assertFalse(root.is_leaf)

        node = root
        level = 0
        while not node.is_leaf:
            level += 1
            node = node.left
            self.assertEqual(node.level, level)
            self.assertEqual(node.width, 1024 >> level)

        self.assertEqual(node.level, 10)
        self.assertTrue(node.is_leaf)
        self.assertEqual(node.width, 1)
        self.assertIsNone(node.start_hash)  # deferred, not set yet

        # Fill it
        block_hash = os.urandom(32)
        tree.set_leaf_start_hash(0, block_hash)
        self.assertEqual(node.start_hash, block_hash)

    def test_internal_nodes_no_start_hash(self):
        """Internal nodes should have start_hash=None."""
        tree = BloomTree()
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))
        tree.insert(0, variants)

        self.assertIsNone(tree.root.start_hash)
        self.assertIsNone(tree.root.left.start_hash)

    def test_deferred_start_hash(self):
        """Leaf start_hash is None after insert, set by next block."""
        tree = BloomTree()
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))

        # Insert first block
        tree.insert(0, variants)

        # Leaf at offset 0 has no start_hash
        leaf = tree.root
        for _ in range(10):
            leaf = leaf.left
        self.assertIsNone(leaf.start_hash)

        # Search returns empty (no leaf hashes to return)
        self.assertEqual(bloom_search(tree.root, variants[0]), [])

        # Next block fills it
        h1 = os.urandom(32)
        tree.set_leaf_start_hash(0, h1)
        self.assertEqual(leaf.start_hash, h1)

        # Now search works
        self.assertEqual(bloom_search(tree.root, variants[0]), [h1])


class TestBloomSearchScoped(unittest.TestCase):
    """A sealed era's bloom nodes are asked from the block whose header holds
    the root; a child by plain hash is in the same block."""

    def test_nodes_are_fetched_scoped_by_the_block(self):
        from unittest import mock

        from astreum.crypto.bloom_tree.tree import bloom_search_storage

        tree = BloomTree()
        block_hash = os.urandom(32)
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))
        tree.insert(0, variants)
        tree.set_leaf_start_hash(0, block_hash)

        stored = {}

        def collect(node):
            if node is None:
                return
            node._expr = None
            collect(node.left)
            collect(node.right)
            expr = node.expr()
            stored[expr.hash()] = expr

        collect(tree.root)
        header_block = os.urandom(32)
        seen = []

        def fake_get_expr(astreum_node, expr_id, contact=None, **kw):
            seen.append(kw.get("block_hash"))
            return stored.get(expr_id)

        with mock.patch("astreum.crypto.bloom_tree.tree.get_expr", side_effect=fake_get_expr):
            hits = bloom_search_storage(
                tree.root.expr().hash(), variants[0], object(), block_hash=header_block
            )

        self.assertEqual(hits, [block_hash])
        self.assertGreater(len(seen), 1)
        self.assertEqual(set(seen), {header_block})


if __name__ == "__main__":
    unittest.main()
