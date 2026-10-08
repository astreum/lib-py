"""Tests for record generation (slotting), the walk size fees are charged on."""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HELPERS_DIR = Path(__file__).resolve().parent.parent / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from astreum.consensus.transaction.storage.initial import (
    _flat_size,
    generate_initial_storage_record,
    storage_walk_fee,
    storage_walk_size,
)
from astreum.consensus.transaction.storage.model import StorageRecord
from tests.consensus.transaction.test_storage_record_refs import flat, store_flat
from astreum.storage.radix import (
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
from astreum.expression import Expr, NIL, int_, bytes_, link, resolve_list_exprs
from astreum.consensus.constants import STORAGE_ADDRESS

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_storage_account,
    store_expr_tree,
)


class TestGenerateInitialStorageRecord(unittest.TestCase):
    """Tests for _slot_if_new duplicate detection via generate_initial_storage_record."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def test_duplicate_subexpr_within_tree_skipped(self):
        """Same atom appearing twice in a tree → slotted once, no gap in sequence."""
        x = int_(42)
        tree = link(x, link(x, NIL))
        store_expr_tree(self.node, tree)

        result = generate_initial_storage_record(self.node, self.block, tree)
        self.assertIsNotNone(result)
        record, slots, found_exprs, fee = result

        # x, link(x, nil), nil = 3 unique exprs (second x silently skipped)
        self.assertEqual(len(slots), 3)
        self.assertEqual(len(set(slots)), 3)
        # in walk order: the head x, then the tail link (its x is already slotted)
        self.assertEqual(slots[0], x.hash())
        self.assertEqual(slots[1], tree._tail.hash())

    def test_subexpr_in_global_storage_goes_to_found(self):
        """Expr already in global radix tree → in found_exprs, not slotted."""
        pre_existing = int_(42)
        new_atom = int_(7)
        tree = link(pre_existing, link(new_atom, NIL))
        store_expr_tree(self.node, tree)

        # Pre-seed the atom in the storage radix tree
        storage_account = self.block.accounts.get_account(STORAGE_ADDRESS, self.node)
        put_in_radix_tree(storage_account.data, self.node, pre_existing.hash(), pre_existing)
        for tn in storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(tn)] = get_radix_node_expr(tn)
        storage_account.data_hash = storage_account.data.root_hash or b""

        result = generate_initial_storage_record(self.node, self.block, tree)
        self.assertIsNotNone(result)
        record, slots, found_exprs, fee = result

        self.assertIn(pre_existing.hash(), found_exprs)
        self.assertNotIn(new_atom.hash(), found_exprs)

    def test_all_unique_subexprs_slotted_sequentially(self):
        """Every unique sub-expr is slotted once, in walk order; found_exprs is empty."""
        a = int_(1)
        b = int_(2)
        c = int_(3)
        tree = link(a, link(b, link(c, NIL)))
        store_expr_tree(self.node, tree)

        result = generate_initial_storage_record(self.node, self.block, tree)
        self.assertIsNotNone(result)
        record, slots, found_exprs, fee = result

        self.assertEqual(len(found_exprs), 0)
        self.assertEqual(len(set(slots)), len(slots))
        self.assertEqual(record.new_count, len(slots))


class TestStorageWalkSize(unittest.TestCase):
    """storage_walk_size: what a sender is charged for an expr they store."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def test_counts_every_distinct_cell_root_included(self):
        x = int_(42)
        tree = link(x, link(x, NIL))
        store_expr_tree(self.node, tree)
        # The record walk (root excluded) slots x and link(x, nil); nil is nothing.
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, tree, follow_refs=True
        )
        self.assertEqual(len(slots), 2)
        size = storage_walk_size(self.node, self.block, tree)
        self.assertEqual(size, sum(
            _flat_size(e, self.node, None) for e in (x, link(x, NIL), tree)
        ))

    def test_same_size_whole_or_fetched_cell_by_cell(self):
        inner = link(bytes_(b"abcdef"), link(int_(7), NIL))
        tree = link(inner, link(bytes_(b"tail-value"), NIL))
        whole = storage_walk_size(self.node, self.block, tree)

        # The same tree as a verifier holds it: one cell, children by hash.
        store_flat(self.node, tree)
        self.assertEqual(
            storage_walk_size(self.node, self.block, flat(tree)), whole
        )

    def test_excluded_hashes_are_neither_counted_nor_fetched(self):
        named = os.urandom(32)  # not in storage: fetching it would fail
        receipt_like = link(Expr("link", head_hash=named), link(int_(1), NIL))
        with_named = storage_walk_size(self.node, self.block, receipt_like, exclude=(named,))
        self.assertIsNotNone(with_named)
        self.assertIsNone(storage_walk_size(self.node, self.block, receipt_like))

    def test_unfetchable_reference_gives_none(self):
        tree = link(Expr("link", head_hash=os.urandom(32)), NIL)
        self.assertIsNone(storage_walk_size(self.node, self.block, tree))
        self.assertIsNone(storage_walk_fee(self.node, self.block, tree))


class TestRecordRoundTrip(unittest.TestCase):
    """A record round-trips through its expr, in the 7-node layout."""

    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        seed_storage_account(self.block)

    def _record(self):
        tree = link(int_(1), NIL)
        store_expr_tree(self.node, tree)
        result = generate_initial_storage_record(self.node, self.block, tree)
        self.assertIsNotNone(result)
        return result[0]

    def test_roundtrip(self):
        record = self._record()
        record.last_payment_winner = os.urandom(32)
        record._expr = None
        self.assertEqual(len(resolve_list_exprs(self.node, record.expr())[0]), 7)
        store_expr_tree(self.node, record.expr())
        decoded = StorageRecord.from_storage(self.node, record.expr().hash())
        self.assertIsNotNone(decoded)
        for name in (
            "creation_block_hash", "last_payment_block_hash", "last_payment_height",
            "last_payment_winner", "new_size", "new_count", "slots_root",
        ):
            self.assertEqual(getattr(decoded, name), getattr(record, name), name)
        self.assertFalse(hasattr(decoded, "mint"))

    def test_slots_root_and_winner_are_bytes_values_not_links(self):
        record = self._record()
        record.last_payment_winner = os.urandom(32)
        record._expr = None
        nodes, _ = resolve_list_exprs(self.node, record.expr())
        # [last_payment_block_hash, height, winner, creation, slots_root, count, size]
        self.assertEqual([n._tag for n in nodes], ["link", "int", "bytes", "link", "bytes", "int", "int"])
        self.assertEqual(nodes[2].value, record.last_payment_winner)
        self.assertEqual(nodes[4].value, record.slots_root)

    def test_older_layouts_are_rejected(self):
        record = self._record()
        nodes, _ = resolve_list_exprs(self.node, record.expr())
        # drop slots_root: the 6-node layout
        six = NIL
        for n in reversed([x for i, x in enumerate(nodes) if i != 4]):
            six = link(n, six)
        # an extra node after the first: 8 nodes
        eight = NIL
        for n in reversed(nodes[:1] + [int_(0)] + nodes[1:]):
            eight = link(n, eight)
        # winner as a hash link, as before
        old_winner = NIL
        swapped = list(nodes)
        swapped[2] = Expr("link", head_hash=record.last_payment_winner)
        for n in reversed(swapped):
            old_winner = link(n, old_winner)
        for old in (six, eight, old_winner):
            store_expr_tree(self.node, old)
            self.assertIsNone(StorageRecord.from_storage(self.node, old.hash()))

    def test_parse_record_new_count_reads_the_record(self):
        from astreum.storage.records import parse_record_new_count

        record = self._record()
        store_expr_tree(self.node, record.expr())
        self.assertEqual(parse_record_new_count(self.node, record.expr()), record.new_count)
        self.assertIsNone(parse_record_new_count(self.node, int_(3)))
        self.assertIsNone(parse_record_new_count(self.node, None))


if __name__ == "__main__":
    unittest.main()
