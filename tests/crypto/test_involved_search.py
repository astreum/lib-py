"""Tests for involved-account bloom elements and the search that uses them."""
import hashlib
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.crypto.bloom_search import (
    bloom_search_tx,
    make_involved_variants,
    make_search_variants,
)
from astreum.crypto.bloom_search import search as search_module
from astreum.crypto.bloom_tree import BloomTree
from astreum.crypto.bloom_tree.tree import bloom_search_leaves, bloom_search_storage
from astreum.expression import NIL

Z = b"\x00" * 32

# A bloom filter has false positives, so a test that expects a miss must not use
# random addresses. Fixed addresses make every run give the same answer.
def _addr(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _store_tree(tree):
    """Collect every node expr of a tree, keyed by hash."""
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
    return stored


def _tx(label: str):
    return SimpleNamespace(
        hash=_addr(label + "/hash"),
        sender=_addr(label + "/sender"),
        recipient=_addr(label + "/recipient"),
        data=NIL,
        block_hash=None,
    )


def _block(tree, height, previous=None):
    return SimpleNamespace(
        height=height,
        bloom_hash=tree.root.expr().hash(),
        expr_id=_addr(f"block/{height}"),
        previous_block=previous,
    )


def _insert_tx(tree, offset, tx, involved=()):
    """Insert a tx the way apply does: 7 base elements and involved elements."""
    tree.insert(offset, make_search_variants(tx.hash, tx.sender, tx.recipient))
    for account, first in involved:
        elements = make_involved_variants(tx.hash, account)
        tree.insert(offset, elements if first else elements[1:])


class TestVariants(unittest.TestCase):

    def test_variants_are_160_bytes(self):
        t, s, r, k = (os.urandom(32) for _ in range(4))
        base = make_search_variants(t, s, r)
        keyed = make_search_variants(t, s, r, k)
        self.assertEqual(len(base), 7)
        self.assertEqual(len(keyed), 4)
        for v in base + keyed:
            self.assertEqual(len(v), 160)
            self.assertEqual(v[128:], Z)

    def test_involved_variants(self):
        t, i = os.urandom(32), os.urandom(32)
        block_element, tx_element = make_involved_variants(t, i)
        self.assertEqual(block_element, Z + Z + Z + Z + i)
        self.assertEqual(tx_element, t + Z + Z + Z + i)

    def test_contract_cannot_write_involved_column(self):
        for _ in range(5):
            variants = make_search_variants(
                os.urandom(32), os.urandom(32), os.urandom(32), os.urandom(32)
            )
            for v in variants:
                self.assertEqual(v[128:160], Z)


class TestSearchLeaves(unittest.TestCase):

    def test_search_leaves_matches_storage(self):
        tree = BloomTree()
        block_hash = os.urandom(32)
        variants = make_search_variants(os.urandom(32), os.urandom(32), os.urandom(32))
        tree.insert(0, variants)
        tree.set_leaf_start_hash(0, block_hash)
        stored = _store_tree(tree)
        root = tree.root.expr().hash()

        def fake_get_expr(astreum_node, expr_id, contact=None, **kw):
            return stored.get(expr_id)

        with mock.patch("astreum.crypto.bloom_tree.tree.get_expr", side_effect=fake_get_expr):
            leaves = bloom_search_leaves(root, variants[0], object())
            hits = bloom_search_storage(root, variants[0], object())
        self.assertEqual([leaf.start_hash for leaf in leaves], [block_hash])
        self.assertEqual(hits, [block_hash])


class TestSearchByInvolved(unittest.TestCase):

    def _search(self, trees_stored, starting_block, txs_by_block, **kw):
        def fake_get_expr(astreum_node, expr_id, contact=None, **kwargs):
            return trees_stored.get(expr_id)

        def fake_load(node, block):
            return txs_by_block[block.expr_id]

        with mock.patch("astreum.crypto.bloom_tree.tree.get_expr", side_effect=fake_get_expr), \
                mock.patch.object(search_module, "_load_block_txs", side_effect=fake_load):
            return bloom_search_tx(object(), starting_block=starting_block, limit=10, **kw)

    def test_search_tx_by_involved_finds_tx(self):
        tree = BloomTree()
        involved = _addr("involved")
        txs = [_tx(f"a{i}") for i in range(3)]
        _insert_tx(tree, 0, txs[0])
        _insert_tx(tree, 0, txs[1], involved=[(involved, True)])
        _insert_tx(tree, 0, txs[2])
        block = _block(tree, 1)
        found = self._search(_store_tree(tree), block, {block.expr_id: txs}, involved=involved)
        self.assertEqual([t.hash for t in found], [txs[1].hash])

    def test_search_tx_by_involved_two_txs_in_one_block(self):
        tree = BloomTree()
        involved = _addr("involved")
        txs = [_tx(f"b{i}") for i in range(3)]
        _insert_tx(tree, 0, txs[0], involved=[(involved, True)])
        _insert_tx(tree, 0, txs[1])
        _insert_tx(tree, 0, txs[2], involved=[(involved, False)])
        block = _block(tree, 1)
        found = self._search(_store_tree(tree), block, {block.expr_id: txs}, involved=involved)
        self.assertEqual({t.hash for t in found}, {txs[0].hash, txs[2].hash})

    def test_search_tx_by_involved_across_era_boundary(self):
        involved = _addr("involved")
        old_tx = _tx("old")
        old_tree = BloomTree()
        _insert_tx(old_tree, 5, old_tx, involved=[(involved, True)])
        old_block = _block(old_tree, 5)

        new_tx = _tx("new")
        new_tree = BloomTree()
        _insert_tx(new_tree, 3, new_tx)
        new_block = _block(new_tree, 1024 + 3, previous=old_block)

        stored = {**_store_tree(old_tree), **_store_tree(new_tree)}
        found = self._search(
            stored, new_block,
            {old_block.expr_id: [old_tx], new_block.expr_id: [new_tx]},
            involved=involved,
        )
        self.assertEqual([t.hash for t in found], [old_tx.hash])
        self.assertEqual(found[0].block_hash, old_block.expr_id)

    def test_search_tx_by_involved_no_match(self):
        tree = BloomTree()
        txs = [_tx("c0"), _tx("c1")]
        _insert_tx(tree, 0, txs[0], involved=[(_addr("other"), True)])
        _insert_tx(tree, 0, txs[1])
        block = _block(tree, 1)
        found = self._search(
            _store_tree(tree), block, {block.expr_id: txs}, involved=_addr("absent")
        )
        self.assertEqual(found, [])

    def test_involved_with_other_filter_raises(self):
        block = SimpleNamespace(height=1, bloom_hash=Z, expr_id=Z, previous_block=None)
        involved = os.urandom(32)
        for kw in ("tx_hash", "sender", "receiver", "key"):
            with self.assertRaises(ValueError):
                bloom_search_tx(
                    object(), starting_block=block, involved=involved,
                    **{kw: os.urandom(32)},
                )

    def test_search_tx_still_finds_sender(self):
        tree = BloomTree()
        txs = [_tx("d0"), _tx("d1")]
        _insert_tx(tree, 0, txs[0], involved=[(_addr("other"), True)])
        _insert_tx(tree, 0, txs[1])
        block = _block(tree, 1)
        found = self._search(
            _store_tree(tree), block, {block.expr_id: txs}, sender=txs[1].sender
        )
        self.assertEqual([t.hash for t in found], [txs[1].hash])


if __name__ == "__main__":
    unittest.main()
