"""Tests for record generation (slotting), the walk size fees are charged on."""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
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
from dataclasses import replace

from astreum.consensus.transaction.storage.model import (
    StorageLane,
    StorageRecord,
    storage_lane_from_expr,
    storage_lane_to_expr,
    storage_record_cells,
    storage_record_from_expr,
    storage_record_from_storage,
    storage_record_to_expr,
)
from tests.unit.consensus.transaction.test_storage_record_refs import flat, store_flat
from astreum.storage.radix import (
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
from astreum.expression import Expr, NIL, int_, bytes_, link, resolve_list_exprs
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.expression import ZERO32

from _helpers import (
    _FakeNode,
    flush_pending,
    make_block,
    make_previous_block,
    seed_storage_account,
    store_expr_tree,
)
from astreum.expression.helpers import get_expr_builtin_tag


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
        self.assertEqual(slots[1], tree.tail.hash())

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
    """A record round-trips through its expr, in the nested 5-item layout."""

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

    def _with_winners(self, record):
        record.lanes = (
            replace(record.lanes[0], winner=os.urandom(32), height=5),
            replace(record.lanes[1], winner=os.urandom(32), height=7),
        )
        record._expr = None
        return record

    def _list(self, items):
        out = NIL
        for item in reversed(items):
            out = link(item, out)
        return out

    def _record_expr(self, lanes, creation=b"\x01" * 32):
        return self._list([
            lanes,
            Expr("link", head_hash=creation),
            bytes_(b"\x02" * 32),
            int_(1),
            int_(1),
        ])

    def test_record_round_trip_two_lanes(self):
        record = self._with_winners(self._record())
        self.assertEqual(len(resolve_list_exprs(self.node, storage_record_to_expr(record))[0]), 5)
        store_expr_tree(self.node, storage_record_to_expr(record))
        decoded = storage_record_from_storage(self.node, storage_record_to_expr(record).hash())
        self.assertIsNotNone(decoded)
        for name in ("creation_block_hash", "lanes", "new_size", "new_count", "slots_root"):
            self.assertEqual(getattr(decoded, name), getattr(record, name), name)
        self.assertEqual(len(decoded.lanes), 2)

    def test_record_layout_types(self):
        record = self._with_winners(self._record())
        nodes, _ = resolve_list_exprs(self.node, storage_record_to_expr(record))
        # [lanes, creation, slots_root, count, size]
        self.assertEqual(
            [get_expr_builtin_tag(n) for n in nodes],
            ["link", "link", "bytes", "int", "int"],
        )
        self.assertEqual(nodes[2].value, record.slots_root)
        lanes, _ = resolve_list_exprs(self.node, nodes[0])
        self.assertEqual(len(lanes), 2)
        for lane_expr, lane in zip(lanes, record.lanes):
            items, _ = resolve_list_exprs(self.node, lane_expr)
            # [block_hash, height, winner]
            self.assertEqual([get_expr_builtin_tag(n) for n in items], ["link", "int", "bytes"])
            self.assertEqual(items[2].value, lane.winner)

    def test_new_record_has_two_empty_lanes(self):
        record = self._record()
        self.assertEqual(len(record.lanes), 2)
        for lane in record.lanes:
            self.assertEqual(lane.winner, ZERO32)
            self.assertEqual(lane.block_hash, self.block.previous_block_hash)
            self.assertEqual(lane.height, self.block.height - 1)
        self.assertEqual(record.lanes[0], record.lanes[1])
        self.assertEqual(
            storage_lane_to_expr(record.lanes[0]).hash(),
            storage_lane_to_expr(record.lanes[1]).hash(),
        )

    def test_equal_lanes_share_one_cell(self):
        record = self._record()
        hashes = [e.hash() for e in storage_record_cells(record)]
        self.assertEqual(len(hashes), len(set(hashes)))
        lane_hash = storage_lane_to_expr(record.lanes[0]).hash()
        self.assertEqual(hashes.count(lane_hash), 1)

    def test_lane_round_trip(self):
        lane = StorageLane(block_hash=b"\x0a" * 32, height=9, winner=b"\x0b" * 32)
        expr = storage_lane_to_expr(lane)
        store_expr_tree(self.node, expr)
        self.assertEqual(storage_lane_from_expr(self.node, expr), lane)

    def test_lane_wrong_item_count_rejected(self):
        hash_item = Expr("link", head_hash=b"\x0a" * 32)
        for items in (
            [hash_item, int_(1)],
            [hash_item, int_(1), bytes_(b"\x0b" * 32), int_(2)],
        ):
            expr = self._list(items)
            store_expr_tree(self.node, expr)
            self.assertIsNone(storage_lane_from_expr(self.node, expr))

    def test_lanes_list_wrong_length_rejected(self):
        lane = storage_lane_to_expr(StorageLane(block_hash=b"\x0a" * 32))
        for count in (1, 3):
            expr = self._record_expr(self._list([lane] * count))
            store_expr_tree(self.node, expr)
            self.assertIsNone(storage_record_from_storage(self.node, expr.hash()), count)

    def test_record_rejects_seven_nodes(self):
        seven = self._list([
            Expr("link", head_hash=b"\x0a" * 32),
            int_(1),
            bytes_(b"\x0b" * 32),
            Expr("link", head_hash=b"\x01" * 32),
            bytes_(b"\x02" * 32),
            int_(1),
            int_(1),
        ])
        store_expr_tree(self.node, seven)
        self.assertIsNone(storage_record_from_storage(self.node, seven.hash()))

    def test_lane_item_types_rejected(self):
        # winner as a hash link instead of a bytes value
        bad = self._list([
            Expr("link", head_hash=b"\x0a" * 32),
            int_(1),
            Expr("link", head_hash=b"\x0b" * 32),
        ])
        store_expr_tree(self.node, bad)
        self.assertIsNone(storage_lane_from_expr(self.node, bad))

    def test_parse_record_new_count_reads_the_record(self):
        from astreum.storage.records import parse_record_new_count

        record = self._record()
        store_expr_tree(self.node, storage_record_to_expr(record))
        self.assertEqual(
            parse_record_new_count(self.node, storage_record_to_expr(record)), record.new_count
        )
        self.assertIsNone(parse_record_new_count(self.node, int_(3)))
        self.assertIsNone(parse_record_new_count(self.node, None))


if __name__ == "__main__":
    unittest.main()
