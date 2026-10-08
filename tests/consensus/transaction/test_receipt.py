"""Receipt layout: seven body fields, no mint."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "src", ROOT, Path(__file__).resolve().parent.parent / "validation"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from astreum.consensus.models.receipt import Receipt, STATUS_FAILED, STATUS_SUCCESS
from astreum.expression import int_, link, resolve_list_exprs

from _helpers import _FakeNode, store_expr_tree


def _receipt(status=STATUS_SUCCESS, node=None) -> Receipt:
    tx_hash = int_(int.from_bytes(os.urandom(4), "big")).hash()
    logs_hash = int_(int.from_bytes(os.urandom(4), "big")).hash()
    if node is not None:
        for h in (tx_hash, logs_hash):
            node.hot_storage[h] = int_(0)
    return Receipt(
        transaction_hash=tx_hash,
        transaction_fee=1,
        storage_fee=700,
        data_fee=3,
        execution_fee=40,
        status=status,
        logs_hash=logs_hash,
    )


class TestReceipt(unittest.TestCase):
    def test_has_no_mint(self):
        self.assertFalse(hasattr(_receipt(), "mint"))

    def test_body_has_seven_fields(self):
        node = _FakeNode()
        body = _receipt(node=node).expr()._head._head
        nodes, missed = resolve_list_exprs(node, body)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 7)

    def test_body_order_and_values(self):
        r = _receipt()
        cells = []
        cell = r.expr()._head._head
        while cell is not None and cell._tail is not None:
            cells.append(cell._head)
            cell = cell._tail
        self.assertEqual(
            [c.value for c in cells],
            [r.data_fee, r.execution_fee, None, r.status, r.storage_fee, r.transaction_fee],
        )
        self.assertEqual(cell._head_hash, r.transaction_hash)

    def test_from_storage_names_the_transaction_whether_or_not_it_is_stored(self):
        for stored in (False, True):
            node = _FakeNode()
            r = _receipt(node=node)
            if stored:
                # The transaction's own cell is in storage: it must not be
                # mistaken for the receipt's last field.
                node.hot_storage[r.transaction_hash] = link(int_(5), int_(6))
            store_expr_tree(node, r.expr())
            loaded = Receipt.from_storage(node, r.expr().hash())
            self.assertEqual(loaded.transaction_hash, r.transaction_hash, stored)
            self.assertEqual(loaded.logs_hash, r.logs_hash)
            self.assertEqual(loaded.total_fee, r.total_fee)

    def test_from_storage_asks_for_nothing_but_the_receipts_own_cells(self):
        node = _FakeNode()
        r = _receipt(node=node)
        store_expr_tree(node, r.expr())
        del node.hot_storage[r.transaction_hash]
        loaded = Receipt.from_storage(node, r.expr().hash())  # the tx need not exist
        self.assertEqual(loaded.transaction_hash, r.transaction_hash)

    def test_eight_field_body_is_rejected(self):
        node = _FakeNode()
        r = _receipt(node=node)
        body = r.expr()._head._head
        old = link(int_(0), body)  # the retired leading mint field
        from astreum.expression import NIL, symbol

        expr = link(link(old, NIL), symbol("receipt"))
        store_expr_tree(node, expr)
        with self.assertRaises(ValueError):
            Receipt.from_storage(node, expr.hash())


if __name__ == "__main__":
    unittest.main()
