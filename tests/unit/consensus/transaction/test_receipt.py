"""Receipt layout: eight body fields, including mint."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
for p in (ROOT / "src", ROOT, Path(__file__).resolve().parent.parent / "validation"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from astreum.consensus.models.receipt import Receipt, STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction.storage.initial import storage_walk_size
from astreum.expression import NIL, Expr, int_, link, resolve_list_exprs, symbol
from astreum.storage.workers.claim_schedule import _RECEIPT_OVERHEAD_BYTES

from _helpers import _FakeNode, store_expr_tree


def _receipt(status=STATUS_SUCCESS, node=None, mint=0) -> Receipt:
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
        mint=mint,
    )


class TestReceipt(unittest.TestCase):
    def test_mint_defaults_to_zero(self):
        self.assertEqual(Receipt(int_(1).hash(), 1, 0, 0, 0, 0).mint, 0)

    def test_body_has_eight_fields(self):
        node = _FakeNode()
        body = _receipt(node=node).expr().head.head
        nodes, missed = resolve_list_exprs(node, body)
        self.assertFalse(missed)
        self.assertEqual(len(nodes), 8)

    def test_body_order_and_values(self):
        r = _receipt(mint=12)
        cells = []
        cell = r.expr().head.head
        while cell is not None and cell.tail is not None:
            cells.append(cell.head)
            cell = cell.tail
        self.assertEqual(
            [c.value for c in cells],
            [r.data_fee, r.execution_fee, None, r.mint, r.status, r.storage_fee, r.transaction_fee],
        )
        self.assertEqual(cell.head_hash, r.transaction_hash)

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

    def test_from_storage_round_trips_mint(self):
        node = _FakeNode()
        r = _receipt(node=node, mint=123_456)
        store_expr_tree(node, r.expr())
        loaded = Receipt.from_storage(node, r.expr().hash())
        self.assertEqual(loaded.mint, 123_456)

    def _store_body(self, node, body):
        expr = link(link(body, NIL), symbol("receipt"))
        store_expr_tree(node, expr)
        return expr.hash()

    def _body(self, node, mint=None, extra=()):
        """The 0.58 body, with a mint cell after the logs cell if given."""
        r = _receipt(node=node)
        body = Expr("link", head_hash=r.transaction_hash)
        cells = [
            int_(r.transaction_fee),
            int_(r.storage_fee),
            int_(r.status),
            *([] if mint is None else [int_(mint)]),
            Expr("link", head_hash=r.logs_hash),
            int_(r.execution_fee),
            int_(r.data_fee),
            *extra,
        ]
        for cell in cells:
            body = link(cell, body)
        return body

    def test_old_body_without_mint_is_rejected(self):
        node = _FakeNode()
        receipt_id = self._store_body(node, self._body(node))
        with self.assertRaises(ValueError):
            Receipt.from_storage(node, receipt_id)

    def test_nine_field_body_is_rejected(self):
        node = _FakeNode()
        body = self._body(node, mint=0, extra=(int_(0),))
        receipt_id = self._store_body(node, body)
        with self.assertRaises(ValueError):
            Receipt.from_storage(node, receipt_id)

    def test_negative_mint_is_rejected(self):
        node = _FakeNode()
        receipt_id = self._store_body(node, self._body(node, mint=-1))
        with self.assertRaises(ValueError):
            Receipt.from_storage(node, receipt_id)

    def test_receipt_overhead_covers_a_claim_receipt(self):
        node = _FakeNode()
        # Distinct values, so no two cells are shared in the walk.
        r = Receipt(
            int_(100).hash(),
            1,
            70_000,
            500,
            4,
            0,
            logs_hash=int_(101).hash(),
            mint=10**12,
        )
        size = storage_walk_size(
            node, None, r.expr(), exclude=(r.transaction_hash, r.logs_hash)
        )
        self.assertIsNotNone(size)
        self.assertLessEqual(size, _RECEIPT_OVERHEAD_BYTES)
        self.assertLess(_RECEIPT_OVERHEAD_BYTES, size + 10)


if __name__ == "__main__":
    unittest.main()
