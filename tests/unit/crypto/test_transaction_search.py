"""Tests for key-based exact matching of transactions."""
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from astreum.crypto.bloom_filter import bloom_insert
from astreum.crypto.bloom_search import make_search_variants
from astreum.crypto.bloom_search import search as search_module
from astreum.crypto.bloom_search.search import _matched_keys, iter_matching_transactions
from astreum.crypto.bloom_tree import BloomTree
from astreum.crypto.bloom_tree.node import BloomNode
from astreum.expression import NIL, ZERO32
from astreum.query import find_transactions

Z = ZERO32

# A bloom filter has false positives, so a test that expects a miss must not use
# random addresses. Fixed addresses make every run give the same answer.
def _addr(label: str) -> bytes:
    return hashlib.sha256(label.encode()).digest()


def _tx(label: str):
    return SimpleNamespace(
        hash=_addr(label + "/hash"),
        sender=_addr(label + "/sender"),
        recipient=_addr(label + "/recipient"),
        data=NIL,
        block_hash=None,
    )


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


def _block(tree, height, previous=None):
    return SimpleNamespace(
        height=height,
        bloom_hash=tree.root.expr().hash(),
        expr_id=_addr(f"block/{height}"),
        previous_block=previous,
    )


def _insert_tx(tree, offset, tx):
    """Insert a tx the way apply does: the 7 base elements."""
    tree.insert(offset, make_search_variants(tx.hash, tx.sender, tx.recipient))


class TestMatchedKeys(unittest.TestCase):

    def test_matched_keys_base(self):
        tx = _tx("a")
        keys = make_search_variants(tx.hash, tx.sender, tx.recipient)
        self.assertEqual(_matched_keys(tx, None, keys), keys)
        # A foreign key that is a candidate must be rejected by the exact
        # membership check, not merely be absent from the candidate list.
        foreign = make_search_variants(tx.hash, _addr("other/sender"), tx.recipient)
        self.assertEqual(foreign[1][128:160], Z)
        self.assertEqual(_matched_keys(tx, None, [foreign[1]]), [])
        self.assertEqual(_matched_keys(tx, None, keys + [foreign[1]]), keys)
        self.assertEqual(_matched_keys(tx, None, [keys[3]]), [keys[3]])

    def test_matched_keys_involved_block_level(self):
        account = _addr("acct")
        tx = _tx("a")
        leaf = BloomNode(level=10)
        bloom_insert(leaf.filter, tx.hash + Z + Z + Z + account)
        key = Z + Z + Z + Z + account
        self.assertEqual(_matched_keys(tx, leaf, [key]), [key])

        other_account = _addr("other")
        other_leaf = BloomNode(level=10)
        bloom_insert(other_leaf.filter, tx.hash + Z + Z + Z + other_account)
        self.assertEqual(_matched_keys(tx, other_leaf, [key]), [])


class TestIterMatchingTransactions(unittest.TestCase):

    def _search(self, trees_stored, starting_block, txs_by_block, *, keys, limit=1):
        def fake_get_expr(astreum_node, expr_id, contact=None, **kwargs):
            return trees_stored.get(expr_id)

        def fake_load(node, block):
            return txs_by_block[block.expr_id]

        with mock.patch("astreum.crypto.bloom_tree.tree.get_expr", side_effect=fake_get_expr), \
                mock.patch.object(search_module, "_load_block_txs", side_effect=fake_load):
            return list(iter_matching_transactions(
                object(), keys=keys, starting_block=starting_block, limit=limit
            ))

    def test_empty_keys_yields_nothing(self):
        tree = BloomTree()
        tx = _tx("empty")
        _insert_tx(tree, 0, tx)
        block = _block(tree, 1)
        found = self._search(
            _store_tree(tree), block, {block.expr_id: [tx]}, keys=[]
        )
        self.assertEqual(found, [])

    def test_yields_tx_and_matched_keys(self):
        tree = BloomTree()
        tx = _tx("y")
        _insert_tx(tree, 0, tx)
        block = _block(tree, 1)
        keys = make_search_variants(tx.hash, tx.sender, tx.recipient)
        found = self._search(
            _store_tree(tree), block, {block.expr_id: [tx]}, keys=keys
        )
        self.assertEqual(len(found), 1)
        yielded_tx, matched = found[0]
        self.assertIs(yielded_tx, tx)
        self.assertEqual(matched, keys)
        self.assertEqual(yielded_tx.block_hash, block.expr_id)

    def test_duplicate_keys_deduped(self):
        tree = BloomTree()
        tx = _tx("dup")
        _insert_tx(tree, 0, tx)
        block = _block(tree, 1)
        sender_key = make_search_variants(tx.hash, tx.sender, tx.recipient)[1]
        found = self._search(
            _store_tree(tree), block, {block.expr_id: [tx]},
            keys=[sender_key, sender_key],
        )
        self.assertEqual(len(found), 1)
        yielded_tx, matched = found[0]
        self.assertIs(yielded_tx, tx)
        self.assertEqual(matched, [sender_key])

    def test_per_key_limit(self):
        tree = BloomTree()
        tx = _tx("pl")
        _insert_tx(tree, 0, tx)
        block = _block(tree, 1)
        keys = make_search_variants(tx.hash, tx.sender, tx.recipient)
        two_keys = [keys[1], keys[5]]
        found = self._search(
            _store_tree(tree), block, {block.expr_id: [tx]},
            keys=two_keys, limit=1,
        )
        self.assertEqual(len(found), 1)
        yielded_tx, matched = found[0]
        self.assertIs(yielded_tx, tx)
        self.assertEqual(matched, two_keys)

    def test_exhausted_key_skips_tx(self):
        tree = BloomTree()
        sender = _addr("shared-sender")
        tx0 = _tx("e0")
        tx1 = _tx("e1")
        tx0.sender = sender
        tx1.sender = sender
        _insert_tx(tree, 0, tx0)
        _insert_tx(tree, 0, tx1)
        block = _block(tree, 1)
        sender_key = make_search_variants(tx0.hash, sender, tx0.recipient)[1]
        found = self._search(
            _store_tree(tree), block, {block.expr_id: [tx0, tx1]},
            keys=[sender_key], limit=1,
        )
        self.assertEqual(len(found), 1)
        yielded_tx, matched = found[0]
        self.assertIs(yielded_tx, tx0)
        self.assertEqual(matched, [sender_key])

    def test_early_termination_when_all_budgets_exhausted(self):
        # Two eras both hold a match for the same key. With limit=1 the search
        # must stop after the newest match instead of walking the whole chain.
        sender = _addr("et-shared-sender")
        new_tx = _tx("et-new")
        old_tx = _tx("et-old")
        new_tx.sender = sender
        old_tx.sender = sender

        old_tree = BloomTree()
        _insert_tx(old_tree, 0, old_tx)
        old_block = _block(old_tree, 100)

        new_tree = BloomTree()
        _insert_tx(new_tree, 0, new_tx)
        new_block = _block(new_tree, 1024, previous=old_block)

        sender_key = make_search_variants(new_tx.hash, sender, new_tx.recipient)[1]

        stored = _store_tree(old_tree)
        stored.update(_store_tree(new_tree))
        txs_by_block = {
            old_block.expr_id: [old_tx],
            new_block.expr_id: [new_tx],
        }

        previous_calls = []

        def fake_get_expr(astreum_node, expr_id, contact=None, **kwargs):
            return stored.get(expr_id)

        def fake_load(node, block):
            return txs_by_block[block.expr_id]

        def fake_get_previous(node, block):
            previous_calls.append(block.expr_id)
            return block.previous_block

        with mock.patch("astreum.crypto.bloom_tree.tree.get_expr", side_effect=fake_get_expr), \
                mock.patch.object(search_module, "_load_block_txs", side_effect=fake_load), \
                mock.patch.object(search_module, "get_previous_block", side_effect=fake_get_previous):
            found = list(iter_matching_transactions(
                object(), keys=[sender_key], starting_block=new_block, limit=1
            ))

        self.assertEqual(len(found), 1)
        yielded_tx, matched = found[0]
        self.assertIs(yielded_tx, new_tx)
        self.assertEqual(matched, [sender_key])
        self.assertEqual(previous_calls, [])


class TestFindTransactions(unittest.TestCase):

    def test_find_transactions_wraps_generator(self):
        node = SimpleNamespace(latest_block=object())
        keys = [b"k"]
        resolved = object()
        canned = [(object(), keys)]
        with mock.patch(
            "astreum.crypto.bloom_search.block_search.find_block_by_height",
            return_value=resolved,
        ) as find_block, mock.patch(
            "astreum.query.iter_matching_transactions",
            return_value=iter(canned),
        ) as delegate:
            found = list(find_transactions(
                node, keys, start_height=5, end_height=1, limit=2
            ))
        find_block.assert_called_once_with(
            node, starting_block=node.latest_block, target_height=5
        )
        delegate.assert_called_once_with(
            node, keys=keys, starting_block=resolved, end_block_height=1, limit=2
        )
        self.assertEqual(found, canned)

    def test_find_transactions_start_height_past_tip(self):
        node = SimpleNamespace(latest_block=object())
        with mock.patch(
            "astreum.crypto.bloom_search.block_search.find_block_by_height",
            return_value=None,
        ), mock.patch(
            "astreum.query.iter_matching_transactions",
        ) as delegate:
            found = list(find_transactions(node, [b"k"], start_height=99))
        self.assertEqual(found, [])
        delegate.assert_not_called()

    def test_find_transactions_default_start_uses_tip(self):
        node = SimpleNamespace(latest_block=object())
        canned = [(object(), [b"k"])]
        with mock.patch(
            "astreum.crypto.bloom_search.block_search.find_block_by_height",
        ) as find_block, mock.patch(
            "astreum.query.iter_matching_transactions",
            return_value=iter(canned),
        ) as delegate:
            found = list(find_transactions(node, [b"k"]))
        find_block.assert_not_called()
        delegate.assert_called_once_with(
            node,
            keys=[b"k"],
            starting_block=node.latest_block,
            end_block_height=0,
            limit=1,
        )
        self.assertEqual(found, canned)


if __name__ == "__main__":
    unittest.main()
