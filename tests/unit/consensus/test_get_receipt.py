"""get_receipt reads one transaction's receipt out of a block's receipts trie."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "src", ROOT, Path(__file__).resolve().parent / "validation"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from astreum.consensus.models.receipt import Receipt, STATUS_SUCCESS
from astreum.expression import ZERO32, int_
from astreum.query import get_receipt
from astreum.storage.radix import (
    RadixTree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.node import radix_node_hash

from _helpers import _FakeNode, store_expr_tree

LOADER = "astreum.consensus.block.encoding.decode.get_block_from_storage"
HOLDER = "astreum.consensus.block.holders.block_holder"


def _receipt(node, mint=0) -> Receipt:
    tx_hash = int_(int.from_bytes(os.urandom(4), "big")).hash()
    logs_hash = int_(int.from_bytes(os.urandom(4), "big")).hash()
    for h in (tx_hash, logs_hash):
        node.hot_storage[h] = int_(0)
    return Receipt(
        transaction_hash=tx_hash,
        transaction_fee=1,
        storage_fee=700,
        data_fee=3,
        execution_fee=40,
        status=STATUS_SUCCESS,
        logs_hash=logs_hash,
        mint=mint,
    )


def _receipts_trie(node, receipts) -> RadixTree:
    tree = RadixTree()
    for r in receipts:
        store_expr_tree(node, r.expr())
        put_in_radix_tree(tree, node, r.transaction_hash, r.expr())
    for trie_node in tree.nodes.values():
        node.hot_storage[radix_node_hash(trie_node)] = get_radix_node_expr(trie_node)
    return tree


class TestGetReceipt(unittest.TestCase):
    def setUp(self):
        self.node = _FakeNode()
        self.block_hash = os.urandom(32)

    def _block(self, receipts_hash):
        return SimpleNamespace(
            receipts_hash=receipts_hash, contact=None, expr_id=os.urandom(32)
        )

    def _get(self, block, tx_hash):
        with mock.patch(LOADER, return_value=block), mock.patch(HOLDER, return_value=None):
            return get_receipt(self.node, self.block_hash, tx_hash)

    def test_returns_receipt_with_mint(self):
        wanted = _receipt(self.node, mint=123_456)
        other = _receipt(self.node, mint=7)
        tree = _receipts_trie(self.node, [wanted, other])
        got = self._get(self._block(tree.root_hash), wanted.transaction_hash)
        self.assertIsInstance(got, Receipt)
        self.assertEqual(got.transaction_hash, wanted.transaction_hash)
        self.assertEqual(got.status, wanted.status)
        self.assertEqual(got.transaction_fee, wanted.transaction_fee)
        self.assertEqual(got.storage_fee, wanted.storage_fee)
        self.assertEqual(got.data_fee, wanted.data_fee)
        self.assertEqual(got.execution_fee, wanted.execution_fee)
        self.assertEqual(got.mint, 123_456)

    def test_tx_not_in_block_returns_none(self):
        tree = _receipts_trie(self.node, [_receipt(self.node), _receipt(self.node)])
        self.assertIsNone(self._get(self._block(tree.root_hash), os.urandom(32)))

    def test_block_without_receipts_returns_none(self):
        self.assertIsNone(self._get(self._block(ZERO32), os.urandom(32)))
        self.assertIsNone(self._get(self._block(None), os.urandom(32)))

    def test_block_load_error_raises(self):
        with mock.patch(LOADER, side_effect=ValueError("no block")), mock.patch(
            HOLDER, return_value=None
        ):
            with self.assertRaises(ValueError):
                get_receipt(self.node, self.block_hash, os.urandom(32))

    def test_exported_at_top_level(self):
        from astreum import get_receipt as exported

        import astreum

        self.assertIs(exported, get_receipt)
        self.assertIn("get_receipt", astreum.__all__)


if __name__ == "__main__":
    unittest.main()
