"""A storage record that follows hash references (the record of a block).

A block's header names its tries only by root hash, so the record of a block
has to walk those references to cover what is new in it. Several nodes build
the same record and each holds the tries differently (whole in memory, or
fetched one cell at a time), so the result must not depend on that.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
HELPERS_DIR = Path(__file__).resolve().parent.parent / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from _helpers import _FakeNode, make_block, make_previous_block, seed_storage_account

from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction.storage.model import storage_record_to_expr
from astreum.consensus.transaction.storage.initial import (
    UnresolvedRecordExpr,
    _flat_size,
    generate_initial_storage_record,
)
from astreum.expression import Expr, NIL, ZERO32, bytes_, int_, link
from astreum.expression.expr import TAG_BYTE_ENCODINGS, _get_head_hash, _get_tail_hash
from astreum.storage.radix import get_radix_node_expr, put_in_radix_tree
from astreum.storage.radix.node import radix_node_hash
from astreum.expression.helpers import get_expr_builtin_tag


def flat(expr):
    """*expr* as a node holds it after fetching: one cell, children as hashes.

    A number or string arrives as its payload with only the hash of the type
    symbol, not as the composite the sender built.
    """
    if expr.base != "link":
        return expr
    if expr.value is not None:
        payload = TAG_BYTE_ENCODINGS[expr.tail.value](expr.value)
        return Expr("link", head=bytes_(payload), tail_hash=expr.tail.hash())
    return Expr("link", head_hash=_get_head_hash(expr), tail_hash=_get_tail_hash(expr))


def store_flat(node, expr):
    """Store every cell of *expr* on its own, as cell-by-cell fetching leaves them."""
    stack = [expr]
    while stack:
        current = stack.pop()
        node.hot_storage[current.hash()] = flat(current)
        if get_expr_builtin_tag(current) == "link":
            for child in (current.head, current.tail):
                if child is not None:
                    stack.append(child)


def points_to(expr, then=NIL):
    """A pair whose head is only the hash of *expr*."""
    return Expr("link", head_hash=expr.hash(), tail=then)


class TestFollowRefs(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.prev_block = make_previous_block()
        self.block = make_block(self.node, self.prev_block)
        self.storage_account = seed_storage_account(self.block)

    def _register(self, expr_id):
        put_in_radix_tree(self.storage_account.data, self.node, expr_id, int_(0))
        for trie_node in self.storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        self.storage_account.data_hash = self.storage_account.data.root_hash or ZERO32

    def _tries(self):
        inner = link(int_(255), link(bytes_(b"\x01\x02"), link(int_(7), NIL)))
        store_flat(self.node, inner)
        return inner, points_to(inner, link(int_(9), NIL))

    def test_references_are_not_followed_by_default(self):
        inner, root = self._tries()
        _, slots, _, _ = generate_initial_storage_record(self.node, self.block, root)
        self.assertNotIn(inner.hash(), slots)

    def test_references_are_followed_and_slotted(self):
        inner, root = self._tries()
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertIn(inner.hash(), slots)
        self.assertIn(inner.tail.hash(), slots)
        self.assertIn(int_(255).hash(), slots)
        self.assertEqual(len(set(slots)), len(slots))

    def test_a_registered_reference_is_not_fetched(self):
        inner, root = self._tries()
        self._register(inner.hash())
        del self.node.hot_storage[inner.hash()]  # would raise if it were fetched
        _, slots, found, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertIn(inner.hash(), found)
        self.assertNotIn(inner.hash(), slots)

    def test_an_unavailable_reference_raises(self):
        inner, root = self._tries()
        del self.node.hot_storage[inner.tail.hash()]
        with self.assertRaises(UnresolvedRecordExpr) as caught:
            generate_initial_storage_record(self.node, self.block, root, follow_refs=True)
        self.assertEqual(caught.exception.expr_id, inner.tail.hash())

    def test_nil_references_are_skipped(self):
        target = link(int_(1), NIL)
        store_flat(self.node, target)
        root = points_to(target)
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertNotIn(ZERO32, slots)

    def test_the_storage_trie_is_followed_like_any_other(self):
        self._register(b"\x33" * 32)
        storage_root = self.storage_account.data.root_hash
        self.assertIsNotNone(storage_root)
        root = points_to(Expr("link", head_hash=storage_root, tail=NIL))
        store_flat(self.node, Expr("link", head_hash=storage_root, tail=NIL))
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertIn(storage_root, slots)

    def test_a_trie_holding_a_paid_record_can_be_walked(self):
        """A record rewritten by a claim names a winner and a root: both bytes
        values, so the next block's walk does not look for them."""
        from astreum.consensus.transaction.storage.model import (
            StorageLane,
            StorageRecord,
            storage_record_to_expr,
        )

        block_a, block_b = b"\x41" * 32, b"\x42" * 32
        record = StorageRecord(
            creation_block_hash=block_a,
            lanes=(
                # an address: no stored expr has this hash
                StorageLane(block_hash=block_b, height=3, winner=b"\x99" * 32),
                StorageLane(block_hash=block_b, height=4, winner=b"\x98" * 32),
            ),
            new_size=10,
            new_count=2,
            slots_root=b"\x77" * 32,  # nor has this
        )
        for key in (block_a, block_b):  # the blocks it names are keys of the trie
            self._register(key)
        put_in_radix_tree(self.storage_account.data, self.node, block_a, storage_record_to_expr(record))
        for trie_node in self.storage_account.data.nodes.values():
            self.node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
        self.storage_account.data_hash = self.storage_account.data.root_hash
        storage_root = self.storage_account.data.root_hash
        store_flat(self.node, Expr("link", head_hash=storage_root, tail=NIL))

        _, slots, found, _ = generate_initial_storage_record(
            self.node, self.block, points_to(Expr("link", head_hash=storage_root, tail=NIL)),
            follow_refs=True,
        )
        # The walk got through the record: its bytes values are cells, not fetches.
        self.assertIn(bytes_(b"\x99" * 32).hash(), slots)
        self.assertIn(bytes_(b"\x98" * 32).hash(), slots)
        self.assertIn(bytes_(b"\x77" * 32).hash(), slots)

    def test_the_storage_trie_is_unavailable_raises(self):
        self._register(b"\x33" * 32)
        storage_root = self.storage_account.data.root_hash
        # Not stored anywhere: it is followed, so it has to be fetched.
        root = Expr("link", head_hash=storage_root, tail=NIL)
        self.node.hot_storage.pop(storage_root, None)
        with self.assertRaises(UnresolvedRecordExpr):
            generate_initial_storage_record(self.node, self.block, root, follow_refs=True)

    def test_held_whole_or_fetched_by_cell_gives_the_same_record(self):
        inner = link(int_(255), link(bytes_(b"\x01\x02"), link(int_(7), link(int_(255), NIL))))
        whole = link(link(inner, NIL), link(int_(9), NIL))

        results = []
        for root in (whole, flat(whole)):
            node = _FakeNode()
            block = make_block(node, self.prev_block)
            seed_storage_account(block)
            store_flat(node, whole)
            results.append(
                generate_initial_storage_record(node, block, root, follow_refs=True)
            )
        (rec_a, slots_a, _, fee_a), (rec_b, slots_b, _, fee_b) = results
        self.assertEqual(list(slots_a), list(slots_b))
        self.assertEqual(rec_a.new_count, rec_b.new_count)
        self.assertEqual(rec_a.new_size, rec_b.new_size)
        self.assertEqual(
            storage_record_to_expr(rec_a).hash(), storage_record_to_expr(rec_b).hash()
        )
        self.assertEqual(fee_a, fee_b)

    def test_collected_exprs_are_single_cells_with_the_same_hash(self):
        inner, root = self._tries()
        collected = {}
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True, collected=collected
        )
        self.assertEqual(set(collected), set(slots))
        for expr_id, expr in collected.items():
            self.assertEqual(expr.hash(), expr_id)
            # No loaded subtree (a number's payload is part of its own cell).
            for child in (expr.head, expr.tail):
                self.assertTrue(child is None or child.base != "link")

    def test_the_record_commits_to_the_walk_order(self):
        from astreum.crypto.merkle import create_merkle_tree

        inner, root = self._tries()
        record, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertEqual(record.new_count, len(slots))
        self.assertEqual(record.slots_root, create_merkle_tree(slots).root)
        # Depth first, head before tail: the referenced list comes first.
        self.assertEqual(slots[0], inner.hash())
        self.assertEqual(slots[1], int_(255).hash())

    def test_the_root_is_the_same_held_whole_or_fetched_by_cell(self):
        inner = link(int_(255), link(bytes_(b"\x01\x02"), link(int_(7), NIL)))
        whole = link(link(inner, NIL), link(int_(9), NIL))
        roots = []
        for root in (whole, flat(whole)):
            node = _FakeNode()
            block = make_block(node, self.prev_block)
            seed_storage_account(block)
            store_flat(node, whole)
            record, slots, _, _ = generate_initial_storage_record(
                node, block, root, follow_refs=True
            )
            roots.append((record.slots_root, slots))
        self.assertEqual(roots[0], roots[1])

    def test_a_number_resolved_after_its_size_was_read_keeps_its_payload_size(self):
        # Fetched as hashes only (size() reads 64 and caches it), then resolved
        # in place when its value is read.
        payload = (255).to_bytes(2, "little")
        number = Expr(
            "link", head_hash=bytes_(payload).hash(), tail_hash=int_(0).tail.hash()
        )
        self.assertEqual(number.size(), 64)
        number.head = bytes_(payload)
        number.head_hash = None
        number.tail = int_(0).tail
        number.tail_hash = None
        self.assertEqual(_flat_size(number, self.node, None), 2)

    def test_a_long_chain_does_not_hit_the_recursion_limit(self):
        # Built from hashes only: a chain this long cannot be built loaded.
        tail_hash = ZERO32
        for value in range(1500):
            number = int_(value)
            self.node.hot_storage[number.hash()] = number
            cell = Expr("link", head_hash=number.hash(), tail_hash=tail_hash)
            self.node.hot_storage[cell.hash()] = cell
            tail_hash = cell.hash()
        root = Expr("link", head_hash=tail_hash, tail=NIL)
        _, slots, _, _ = generate_initial_storage_record(
            self.node, self.block, root, follow_refs=True
        )
        self.assertGreaterEqual(len(slots), 1500)


if __name__ == "__main__":
    unittest.main()
