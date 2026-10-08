from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import ZERO32, int_, bytes_
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.account import Account
from astreum.consensus.models.accounts import Accounts
from astreum.storage.radix import RadixTree, put_in_radix_tree, exists_in_radix_tree
from astreum.storage.admission import is_expr_in_latest_block


import itertools

_block_ids = itertools.count(1000)


class _FakeNode:
    pass


def _storage_account() -> Account:
    return Account(
        balance=0,
        code_hash=ZERO32,
        counter=0,
        data_hash=ZERO32,
        channels_hash=ZERO32,
        data=RadixTree(root_hash=None),
        channels=RadixTree(root_hash=None),
    )


def _latest_block_with(expr_ids: list[bytes]):
    """Build a fake node whose latest block has a storage account data trie
    containing *expr_ids* (populated in-memory, no storage reads)."""
    node = _FakeNode()
    storage_account = _storage_account()
    for eid in expr_ids:
        put_in_radix_tree(storage_account.data, node, eid, int_(1))
    accounts = Accounts()
    accounts.set_account(STORAGE_ADDRESS, storage_account)
    # The admission lookup is cached per block hash: each fake block needs its own.
    block_id = next(_block_ids).to_bytes(32, "big")
    block = type("_Block", (), {"expr_id": block_id, "accounts": accounts})()
    node.latest_block = block
    return node


class TestExistsInRadixTree(unittest.TestCase):
    def setUp(self) -> None:
        self.node = _FakeNode()
        self.tree = RadixTree(root_hash=None)

    def test_empty_tree_returns_false(self) -> None:
        self.assertFalse(exists_in_radix_tree(self.tree, self.node, b"\xaa" * 32))

    def test_bytes_leaf_value(self) -> None:
        key = b"\xaa" * 32
        put_in_radix_tree(self.tree, self.node, key, bytes_(b"val"))
        self.assertTrue(exists_in_radix_tree(self.tree, self.node, key))

    def test_expr_leaf_value(self) -> None:
        key = b"\xbb" * 32
        put_in_radix_tree(self.tree, self.node, key, int_(7))
        self.assertTrue(exists_in_radix_tree(self.tree, self.node, key))

    def test_missing_key_returns_false(self) -> None:
        put_in_radix_tree(self.tree, self.node, b"\xcc" * 32, int_(1))
        self.assertFalse(exists_in_radix_tree(self.tree, self.node, b"\xdd" * 32))

    def test_does_not_materialize_value(self) -> None:
        # Storing a value whose lookup would be unresolved still reports
        # existence: the walk stops at the leaf without get_expr on the value.
        key = b"\xee" * 32
        put_in_radix_tree(self.tree, self.node, key, b"\x00" * 32)
        self.assertTrue(exists_in_radix_tree(self.tree, self.node, key))


class TestIsExprInLatestBlock(unittest.TestCase):
    def test_committed_returns_true(self) -> None:
        eid = b"\x11" * 32
        node = _latest_block_with([eid])
        self.assertTrue(is_expr_in_latest_block(node, eid))

    def test_uncommitted_returns_false(self) -> None:
        node = _latest_block_with([b"\x22" * 32])
        self.assertFalse(is_expr_in_latest_block(node, b"\x33" * 32))

    def test_no_latest_block_fails_closed(self) -> None:
        node = _FakeNode()
        node.latest_block = None
        self.assertFalse(is_expr_in_latest_block(node, b"\x44" * 32))

    def test_no_storage_account_fails_closed(self) -> None:
        node = _FakeNode()
        accounts = Accounts()
        block = type("_Block", (), {"expr_id": b"\x01" * 32, "accounts": accounts})()
        node.latest_block = block
        self.assertFalse(is_expr_in_latest_block(node, b"\x55" * 32))


class TestOneRecordPerBlock(unittest.TestCase):
    """The storage trie holds only block record ids, so a block's inner exprs
    (txs, receipts, values) are not admitted and its record id is."""

    def test_block_record_id_is_admitted_and_inner_expr_is_not(self) -> None:
        record_id = b"\x66" * 32
        inner_expr_id = b"\x77" * 32  # a tx cell inside that block
        node = _latest_block_with([record_id])
        self.assertTrue(is_expr_in_latest_block(node, record_id))
        self.assertFalse(is_expr_in_latest_block(node, inner_expr_id))


if __name__ == "__main__":
    unittest.main()
