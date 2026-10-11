"""Refs to older cells in tries (docs/plans/2026-10-08-refs-in-tries.md).

A small chain is committed block by block. The record of a block is the walk of
its root (``walk_record_slots``, the same walk the block record is built from),
so it holds the cells that are new in the block. A provider per block serves
only the cells of that block's record, which makes every scoped GET a node
makes checkable: it must name the block whose record holds the cell.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
HELPERS_DIR = ROOT / "tests" / "consensus" / "validation"
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from _helpers import _FakeNode, store_expr_tree

from astreum.consensus.account import create_account
from astreum.consensus.models.accounts import Accounts
from astreum.consensus.transaction.storage.initial import walk_record_slots
from astreum.consensus.transaction.treasury.record import (
    TreasuryGuarantee,
    TreasuryLoanRecord,
    TreasuryUserRecord,
)
from astreum.crypto.bloom_tree import BloomTree, bloom_search, read_bloom_node
from astreum.crypto.bloom_tree.tree import bloom_search_storage
from astreum.expression import Expr, NIL, ZERO32, bytes_, int_
from astreum.expression.expr import TAG_BYTE_ENCODINGS, _get_head_hash, _get_tail_hash
from astreum.expression.refs import ref_expr, stored_ref_target
from astreum.storage.radix import (
    RadixTree,
    get_all_from_radix_tree,
    get_from_radix_tree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash
from astreum.expression.helpers import get_expr_builtin_tag


def flat(expr):
    """*expr* as a node holds it after fetching: one cell, children as hashes."""
    if expr.base != "link":
        return expr
    if expr.value is not None:
        payload = TAG_BYTE_ENCODINGS[expr.tail.value](expr.value)
        return Expr("link", head=bytes_(payload), tail_hash=expr.tail.hash())
    return Expr("link", head_hash=_get_head_hash(expr), tail_hash=_get_tail_hash(expr))


class Chain:
    """Blocks committed in turn; each block's provider holds its record."""

    def __init__(self):
        self.node = _FakeNode()  # the node that builds every block
        self.records: dict[bytes, set[bytes]] = {}
        self.roots: dict[bytes, bytes] = {}
        self.node_hashes: dict[bytes, set[bytes]] = {}
        self.height = 0
        # Block ids a header may name: opaque to the walk, as in the storage trie.
        self.block_ids = {b"\xaa" * 32, b"\xbb" * 32}

    def block_id(self) -> bytes:
        self.height += 1
        return self.height.to_bytes(32, "big")

    def commit(self, tree: RadixTree, block_id: bytes | None = None) -> bytes:
        """Store the tree's cells, record what is new in the block, and return
        the block id."""
        block_id = block_id or self.block_id()
        for n in tree.nodes.values():
            store_expr_tree(self.node, get_radix_node_expr(n))
        self.node_hashes[block_id] = {radix_node_hash(n) for n in tree.nodes.values()}
        self.record(block_id, tree.root_hash)
        return block_id

    def record(self, block_id: bytes, root_hash: bytes) -> None:
        header = Expr("link", head_hash=root_hash, tail=NIL)
        slots, _, _ = walk_record_slots(
            self.node, header, self.lookup(), follow_refs=True
        )
        self.records[block_id] = set(slots) | {root_hash}
        self.roots[block_id] = root_hash

    def lookup(self) -> RadixTree:
        tree = RadixTree()
        for bid in self.block_ids | set(self.records):
            put_in_radix_tree(tree, self.node, bid, int_(0))
        return tree

    def reload(self, block_id: bytes) -> RadixTree:
        """The state of a committed block, nothing loaded."""
        return RadixTree(root_hash=self.roots[block_id], root_block=block_id)


class Remote:
    """A node that holds nothing and asks providers with scoped GETs."""

    def __init__(self, chain: Chain):
        self.chain = chain
        self.node = _FakeNode()
        self.gets: list[tuple[bytes, bytes | None]] = []
        self.wrong: list[tuple[bytes, bytes | None]] = []

    def __enter__(self):
        def serve(node, expr_id, resolution=0, contact=None, block_hash=None):
            self.gets.append((expr_id, block_hash))
            if block_hash is None or expr_id not in self.chain.records.get(block_hash, ()):
                self.wrong.append((expr_id, block_hash))
                return None
            cell = flat(self.chain.node.hot_storage[expr_id])
            node.hot_storage[expr_id] = cell
            return cell

        self.patch = mock.patch("astreum.storage.exprs.cascade.get_expr_from_network", serve)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


def keys(n, seed=1):
    return [os.urandom(32) for _ in range(n)]


class TestRadixRefs(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.node = self.chain.node
        tree = RadixTree()
        self.keys = keys(64)
        for i, k in enumerate(self.keys):
            put_in_radix_tree(tree, self.node, k, int_(i))
        self.a = self.chain.commit(tree)

    def write(self, base: bytes, writes: dict[bytes, object]) -> tuple[bytes, RadixTree]:
        tree = self.chain.reload(base)
        for k, v in writes.items():
            put_in_radix_tree(tree, self.node, k, v)
        return self.chain.commit(tree), tree

    def test_the_next_record_holds_the_path_and_refs_not_the_subtrees(self):
        b, _ = self.write(self.a, {self.keys[5]: int_(1000)})
        full = len(self.chain.records[self.a])
        diff = len(self.chain.records[b])
        self.assertGreater(full, 64 * 3)
        self.assertLess(diff, full // 4, (diff, full))
        # No node of the first block comes back: its subtrees are behind refs.
        self.assertEqual(self.chain.records[b] & self.chain.node_hashes[self.a], set())

    def test_a_write_of_the_same_value_changes_nothing(self):
        tree = self.chain.reload(self.a)
        root = tree.root_hash
        put_in_radix_tree(tree, self.node, self.keys[3], int_(3))
        self.assertEqual(tree.root_hash, root)
        self.assertEqual(tree.root_block, self.a)

    def test_pointers_to_untouched_siblings_are_refs_to_their_block(self):
        b, tree = self.write(self.a, {self.keys[7]: int_(99)})
        root = tree.nodes[tree.root_hash]
        refs = [
            (h, blk)
            for h, blk in ((root.child_0, root.child_0_block), (root.child_1, root.child_1_block))
            if h is not None
        ]
        self.assertTrue(refs)
        # One side holds the written key (new, plain); the other is untouched.
        self.assertIn(None, [blk for _, blk in refs])
        self.assertIn(self.a, [blk for _, blk in refs])
        cell = self.node.hot_storage[tree.root_hash]
        found = []
        stack = [cell]
        while stack:
            c = stack.pop()
            if get_expr_builtin_tag(c) != "link":
                continue
            target = stored_ref_target(c)
            if target is not None:
                found.append(target)
                continue
            for part in (c.head, c.tail):
                if part is not None:
                    stack.append(part)
        self.assertIn((self.a, [h for h, blk in refs if blk == self.a][0]), found)

    def test_no_ref_points_to_a_ref(self):
        block = self.a
        for i in range(4):
            block, _ = self.write(block, {self.keys[i * 9]: int_(500 + i)})
        for committed in (block,):
            tree = self.chain.reload(committed)
            get_all_from_radix_tree(tree, self.node)
            for n in tree.nodes.values():
                for h, blk in ((n.child_0, n.child_0_block), (n.child_1, n.child_1_block)):
                    if blk is None or h is None:
                        continue
                    target = self.node.hot_storage[h]
                    self.assertIsNone(stored_ref_target(target), "a ref names a ref")
                    self.assertIn(h, self.chain.records[blk], "ref names the wrong block")

    def test_every_ref_names_the_block_that_holds_the_cell(self):
        block = self.a
        for i in range(5):
            block, _ = self.write(block, {self.keys[i]: int_(700 + i)})
        tree = self.chain.reload(block)
        entries = get_all_from_radix_tree(tree, self.node)
        self.assertEqual(len(entries), 64)
        for n in tree.nodes.values():
            for h, blk in (
                (n.child_0, n.child_0_block),
                (n.child_1, n.child_1_block),
                (n.value if isinstance(n.value, bytes) else None, n.value_block),
            ):
                if h is not None and blk is not None:
                    self.assertIn(h, self.chain.records[blk])

    def test_a_split_leaf_keeps_its_value_by_ref(self):
        chain = Chain()
        tree = RadixTree()
        k1 = b"\x00" + b"\x11" * 31
        put_in_radix_tree(tree, chain.node, k1, int_(11))
        first = chain.commit(tree)
        tree = chain.reload(first)
        k2 = b"\x00" + b"\x11" * 30 + b"\x12"  # shares a long prefix with k1
        put_in_radix_tree(tree, chain.node, k2, int_(12))
        second = chain.commit(tree)

        reloaded = chain.reload(second)
        self.assertEqual(get_from_radix_tree(reloaded, chain.node, k1).hash(), int_(11).hash())
        self.assertEqual(get_from_radix_tree(reloaded, chain.node, k2).hash(), int_(12).hash())
        moved = [n for n in reloaded.nodes.values() if n.value is not None and n.value_block == first]
        self.assertEqual(len(moved), 1, "the old leaf's value is a ref to the first block")
        self.assertIn(int_(11).hash(), chain.records[first])
        self.assertNotIn(int_(11).hash(), chain.records[second])

    def test_same_slots_whole_and_cell_by_cell(self):
        b, tree = self.write(self.a, {self.keys[1]: int_(41), self.keys[2]: int_(42)})
        whole = self.chain.records[b]
        # The same walk on a node that holds every cell on its own.
        flat_node = _FakeNode()
        for h, e in self.node.hot_storage.items():
            flat_node.hot_storage[h] = flat(e)
        header = Expr("link", head_hash=tree.root_hash, tail=NIL)
        slots, _, _ = walk_record_slots(flat_node, header, RadixTree(), follow_refs=True)
        self.assertEqual(set(slots) | {tree.root_hash}, whole)
        self.assertEqual(len(slots), len(set(slots)))

    def test_a_node_holding_nothing_reads_old_keys_through_refs(self):
        block = self.a
        for i in range(4):
            block, _ = self.write(block, {self.keys[i]: int_(900 + i)})
        with Remote(self.chain) as remote:
            tree = RadixTree(root_hash=self.chain.roots[block], root_block=block)
            # written in the first block, several blocks ago
            old = get_from_radix_tree(tree, remote.node, self.keys[40])
            self.assertEqual(old.hash(), int_(40).hash())
            new = get_from_radix_tree(tree, remote.node, self.keys[2])
            self.assertEqual(new.hash(), int_(902).hash())
            self.assertEqual(remote.wrong, [])
            self.assertTrue(remote.gets)
            self.assertIn(self.a, {b for _, b in remote.gets})
            self.assertIn(block, {b for _, b in remote.gets})

    def test_reading_everything_from_nothing_asks_the_right_blocks(self):
        block = self.a
        for i in range(3):
            block, _ = self.write(block, {self.keys[10 + i]: int_(300 + i)})
        with Remote(self.chain) as remote:
            tree = RadixTree(root_hash=self.chain.roots[block], root_block=block)
            entries = get_all_from_radix_tree(tree, remote.node)
            self.assertEqual(len(entries), 64)
            self.assertEqual(remote.wrong, [])


class TestAccountRefs(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.node = self.chain.node
        self.addresses = keys(8)
        accounts = Accounts()
        for i, address in enumerate(self.addresses):
            account = create_account(balance=100 + i)
            put_in_radix_tree(account.data, self.node, b"k" * 32, int_(i))
            accounts.set_account(address, account)
        accounts.update_trie(self.node)
        self.commit(accounts)
        self.first = self.chain.height_block
        self.accounts0 = accounts

    def commit(self, accounts: Accounts) -> None:
        block = self.chain.block_id()
        for n in accounts._trie.nodes.values():
            store_expr_tree(self.node, get_radix_node_expr(n))
        for account in accounts._cache.values():
            store_expr_tree(self.node, account.expr())
            for n in list(account.data.nodes.values()) + list(account.channels.nodes.values()):
                store_expr_tree(self.node, get_radix_node_expr(n))
        self.chain.record(block, accounts._trie.root_hash)
        self.chain.height_block = block

    def next_block(self, base: bytes, root: bytes) -> Accounts:
        return Accounts(root_hash=root, block_hash=base)

    def test_a_balance_change_points_to_the_data_root_by_ref(self):
        accounts = Accounts(root_hash=self.accounts0.root_hash, block_hash=self.first)
        account = accounts.get_account(self.addresses[0], self.node)
        data_root = account.data_hash
        self.assertNotEqual(data_root, ZERO32)
        self.assertEqual(account.data_block, self.first)
        account.balance += 5
        accounts.update_trie(self.node)
        target = stored_ref_target(account.expr().head)
        self.assertEqual(target, (self.first, data_root))
        self.assertEqual(account.expr().tail.tail.tail.head.hash(), Expr("link", head_hash=ZERO32).hash())

    def test_a_changed_data_trie_is_written_in_place(self):
        accounts = Accounts(root_hash=self.accounts0.root_hash, block_hash=self.first)
        account = accounts.get_account(self.addresses[1], self.node)
        put_in_radix_tree(account.data, self.node, b"z" * 32, int_(77))
        accounts.update_trie(self.node)
        self.assertIsNone(account.data_block)
        self.assertIsNone(stored_ref_target(account.expr().head))

    def test_an_account_only_read_is_not_written_back(self):
        accounts = Accounts(root_hash=self.accounts0.root_hash, block_hash=self.first)
        accounts.get_account(self.addresses[2], self.node)
        accounts.get_account(self.addresses[3], self.node)
        self.assertEqual(accounts.update_trie(self.node), self.accounts0.root_hash)

    def test_the_account_is_read_back_with_the_block_of_each_root(self):
        accounts = Accounts(root_hash=self.accounts0.root_hash, block_hash=self.first)
        account = accounts.get_account(self.addresses[4], self.node)
        account.balance += 1
        accounts.update_trie(self.node)
        self.commit(accounts)
        second = self.chain.height_block

        again = Accounts(root_hash=accounts._trie.root_hash, block_hash=second)
        loaded = again.get_account(self.addresses[4], self.node)
        self.assertEqual(loaded.balance, 105)
        self.assertEqual(loaded.data_block, self.first)
        self.assertEqual(loaded.data_hash, account.data_hash)
        self.assertEqual(get_from_radix_tree(loaded.data, self.node, b"k" * 32).hash(), int_(4).hash())
        untouched = again.get_account(self.addresses[5], self.node)
        self.assertEqual(untouched.balance, 105)
        self.assertEqual(untouched.data_block, self.first)

    def test_a_node_holding_nothing_reads_accounts_and_their_data(self):
        accounts = Accounts(root_hash=self.accounts0.root_hash, block_hash=self.first)
        for i in (0, 1):
            accounts.get_account(self.addresses[i], self.node).balance += 10
        accounts.update_trie(self.node)
        self.commit(accounts)
        second = self.chain.height_block
        with Remote(self.chain) as remote:
            reader = Accounts(root_hash=accounts._trie.root_hash, block_hash=second)
            moved = reader.get_account(self.addresses[0], remote.node)
            still = reader.get_account(self.addresses[6], remote.node)
            self.assertEqual(moved.balance, 110)
            self.assertEqual(still.balance, 106)
            self.assertEqual(
                get_from_radix_tree(still.data, remote.node, b"k" * 32).hash(), int_(6).hash()
            )
            self.assertEqual(
                get_from_radix_tree(moved.data, remote.node, b"k" * 32).hash(), int_(0).hash()
            )
            self.assertEqual(remote.wrong, [])


class TestBloomRefs(unittest.TestCase):
    def setUp(self):
        self.chain = Chain()
        self.node = self.chain.node
        self.elements = [os.urandom(160) for _ in range(3)]
        self.node_hashes: dict[bytes, set[bytes]] = {}

    def store(self, tree: BloomTree) -> None:
        for expr in tree.new_exprs():
            store_expr_tree(self.node, expr)

    def commit_tree(self, tree: BloomTree) -> bytes:
        block = self.chain.block_id()
        tree.rewrite_root()
        self.store(tree)
        self.node_hashes[block] = {e.hash() for e in tree.new_exprs()}
        root = tree.root.expr().hash()
        self.chain.record(block, root)
        return block

    def build(self):
        first = BloomTree()
        first.insert(0, [self.elements[0]])
        a = self.commit_tree(first)

        second = BloomTree(first.root.expr().hash(), self.node, block_hash=a)
        second.set_leaf_start_hash(0, b"\xaa" * 32, self.node)
        second.insert(1, [self.elements[1]])
        b = self.commit_tree(second)

        third = BloomTree(second.root.expr().hash(), self.node, block_hash=b)
        third.set_leaf_start_hash(1, b"\xbb" * 32, self.node)
        c = self.commit_tree(third)
        return a, b, c, third

    def test_unchanged_children_are_refs_to_their_block(self):
        a, b, c, third = self.build()
        root = third.root
        for child, block in ((root._left_hash, root._left_block), (root._right_hash, root._right_block)):
            if child is not None:
                self.assertIn(block, (a, b), "an untouched child is a ref to an older block")
        # The root is a cell of the last block, though nothing was inserted.
        self.assertIn(third.root.expr().hash(), self.chain.records[c])

    def test_the_record_of_a_block_does_not_hold_the_older_nodes(self):
        a, b, c, _ = self.build()
        # Small cells (a count, an empty tier) are slotted again; nodes are not.
        self.assertEqual(self.chain.records[c] & self.node_hashes[a], set())
        self.assertEqual(self.chain.records[c] & self.node_hashes[b], set())

    def test_a_node_holding_nothing_searches_through_refs(self):
        a, b, c, third = self.build()
        with Remote(self.chain) as remote:
            found = bloom_search_storage(
                self.chain.roots[c], self.elements[0], remote.node, block_hash=c
            )
            self.assertEqual(found, [b"\xaa" * 32])
            found = bloom_search_storage(
                self.chain.roots[c], self.elements[1], remote.node, block_hash=c
            )
            self.assertEqual(found, [b"\xbb" * 32])
            self.assertEqual(
                bloom_search_storage(self.chain.roots[c], os.urandom(160), remote.node, block_hash=c),
                [],
            )
            self.assertEqual(remote.wrong, [])

    def test_a_loaded_tree_keeps_a_searchable_in_memory_shape(self):
        a, b, c, _ = self.build()
        loaded = BloomTree(self.chain.roots[c], self.node, block_hash=c)
        loaded.insert(5, [self.elements[2]], self.node)
        loaded.set_leaf_start_hash(5, b"\xcc" * 32, self.node)
        self.assertEqual(bloom_search(loaded.root, self.elements[2]), [b"\xcc" * 32])
        self.store(loaded)
        again = read_bloom_node(self.node, loaded.root.expr().hash(), level=0)
        self.assertEqual(again.filter.count, loaded.root.filter.count)
        self.assertTrue(again._left_hash or again._right_hash)


class TestTreasuryRefs(unittest.TestCase):
    def test_a_trie_root_survives_as_a_ref(self):
        node = _FakeNode()
        loans, guarantees, block = os.urandom(32), os.urandom(32), os.urandom(32)
        record = TreasuryUserRecord(
            balance=5,
            loans_root_hash=loans,
            guarantees_root_hash=guarantees,
            loans_root_block=block,
        )
        expr = record.expr()
        store_expr_tree(node, expr)
        loaded = TreasuryUserRecord.from_storage(node, expr.hash(), block_hash=b"\x01" * 32)
        self.assertEqual(loaded.loans_root_hash, loans)
        self.assertEqual(loaded.loans_root_block, block)
        self.assertEqual(loaded.guarantees_root_hash, guarantees)
        # held by a plain hash: in the block that holds the record
        self.assertEqual(loaded.guarantees_root_block, b"\x01" * 32)
        self.assertEqual(loaded.balance, 5)

    def test_an_empty_root_has_no_block(self):
        node = _FakeNode()
        expr = TreasuryUserRecord(balance=3).expr()
        store_expr_tree(node, expr)
        loaded = TreasuryUserRecord.from_storage(node, expr.hash(), block_hash=b"\x01" * 32)
        self.assertEqual(loaded.loans_root_hash, ZERO32)
        self.assertIsNone(loaded.loans_root_block)

    def test_addresses_in_records_are_bytes_the_walk_can_pass(self):
        node = _FakeNode()
        loan = TreasuryLoanRecord(
            creation_block_number=1,
            loan_type=0,
            discounted_amount=5,
            payment_amount=1,
            payment_interval_blocks=1,
            next_payment_block_number=2,
            payment_count=5,
            owner=b"\x55" * 32,
            claimed_guarantees=[(b"\x66" * 32, b"\x77" * 32, 3)],
        )
        guarantee = TreasuryGuarantee(amount=1, duration=1, price=0, expiry=9, claimed_by=b"\x88" * 32)
        for record in (loan, guarantee):
            expr = record.expr()
            store_expr_tree(node, expr)
            slots, _, _ = walk_record_slots(
                node, Expr("link", head_hash=expr.hash(), tail=NIL), RadixTree(), follow_refs=True
            )
            self.assertTrue(slots)
        self.assertEqual(TreasuryLoanRecord.from_storage(node, loan.expr().hash()).owner, b"\x55" * 32)
        self.assertEqual(
            TreasuryLoanRecord.from_storage(node, loan.expr().hash()).claimed_guarantees,
            [(b"\x66" * 32, b"\x77" * 32, 3)],
        )
        self.assertEqual(
            TreasuryGuarantee.from_storage(node, guarantee.expr().hash()).claimed_by, b"\x88" * 32
        )


if __name__ == "__main__":
    unittest.main()
