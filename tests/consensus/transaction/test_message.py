from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import os

from astreum.expression import resolve_inner_exprs
from astreum.consensus.transaction.message import (
    decode_transaction_message,
    encode_transaction_message,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from tests.consensus.transaction.test_apply import (
    _FakeNode,
    _make_block,
    _make_previous_block,
    _make_tx,
    _seed_sender_account,
    _seed_storage_account,
)


class TestTransactionMessage(unittest.TestCase):
    def setUp(self) -> None:
        self.node = _FakeNode()
        self.block = _make_block(self.node, _make_previous_block())
        _seed_storage_account(self.block)
        self.sender_pk, self.sender_key = _seed_sender_account(
            self.block, balance=1_000_000
        )

    def _roundtrip(self, tx):
        tx_exprs, missed = resolve_inner_exprs(self.node, tx.expr())
        self.assertEqual(missed, [])
        payload = encode_transaction_message(tx_exprs)
        decoded = decode_transaction_message(payload)
        self.assertIsNotNone(decoded)
        return decoded, payload

    def _payload(self, tx) -> bytes:
        tx_exprs, missed = resolve_inner_exprs(self.node, tx.expr())
        self.assertEqual(missed, [])
        return encode_transaction_message(tx_exprs)

    def test_encode_skips_builtin_type_symbols(self) -> None:
        tx = _make_tx(
            chain_id=1,
            sender_pk=self.sender_pk,
            recipient=os.urandom(32),
            amount=100_000,
            secret_key=self.sender_key,
        )
        payload = self._payload(tx)
        self.assertNotIn(b"\x01int", payload)
        self.assertNotIn(b"\x01str", payload)

    def test_encode_dedups_expressions(self) -> None:
        tx = _make_tx(
            chain_id=1,
            sender_pk=self.sender_pk,
            recipient=os.urandom(32),
            amount=100_000,
            secret_key=self.sender_key,
        )
        tx_exprs, missed = resolve_inner_exprs(self.node, tx.expr())
        self.assertEqual(missed, [])
        root = tx_exprs[0]
        self.assertEqual(
            encode_transaction_message([root]),
            encode_transaction_message([root, root]),
        )

    def test_encode_emits_value_bytes_once(self) -> None:
        recipient = os.urandom(32)
        tx = _make_tx(
            chain_id=1,
            sender_pk=self.sender_pk,
            recipient=recipient,
            amount=100_000,
            secret_key=self.sender_key,
        )
        payload = self._payload(tx)
        self.assertEqual(payload.count(b"\x02" + recipient), 1)

    def test_transfer_roundtrip(self) -> None:
        tx = _make_tx(
            chain_id=1,
            sender_pk=self.sender_pk,
            recipient=os.urandom(32),
            amount=100_000,
            secret_key=self.sender_key,
        )
        expected = tx.expr().hash()
        decoded, payload = self._roundtrip(tx)
        self.assertEqual(decoded.hash, expected)
        self.assertEqual(decoded.sender, tx.sender)
        self.assertEqual(decoded.recipient, tx.recipient)
        self.assertEqual(decoded.amount, tx.amount)
        self.assertEqual(decoded.body_hash, tx.body_hash)
        self.assertEqual(decoded.expr().hash(), expected)

    def test_decode_garbage_returns_none(self) -> None:
        self.assertIsNone(decode_transaction_message(b""))
        self.assertIsNone(decode_transaction_message(b"\x00"))
        self.assertIsNone(decode_transaction_message(b"\x01\x00\x00\x00\x05hello"))
        # Well-formed page header but a multi-page message: not a transaction
        self.assertIsNone(decode_transaction_message(b"\x01\x00\x01\x00\x02\x00\x01"))
        self.assertIsNone(decode_transaction_message(b"\x99junkjunkjunk"))

    def test_decode_nontx_header_returns_none(self) -> None:
        # A validly-framed payload whose root tail is not symbol("transaction")
        from astreum.expression import Expr, NIL, symbol
        from astreum.communication.storage_response.storage_found import encode_found_pages

        root = Expr("link", head=Expr("link", head=NIL, tail=NIL), tail=symbol("blob"))
        payload = encode_found_pages([root])[0]
        self.assertIsNone(decode_transaction_message(payload))

    def test_apply_uses_whole_message(self) -> None:
        from astreum.consensus.transaction.apply import apply_transaction_obj
        from astreum.consensus.models.receipt import STATUS_SUCCESS

        recipient = os.urandom(32)
        tx = _make_tx(
            chain_id=1,
            sender_pk=self.sender_pk,
            recipient=recipient,
            amount=100_000,
            secret_key=self.sender_key,
        )
        decoded, _ = self._roundtrip(tx)
        apply_transaction_obj(self.node, self.block, decoded)
        receipt = self.block.receipts[-1]
        self.assertEqual(receipt.status, STATUS_SUCCESS)
        self.assertEqual(
            self.block.accounts.get_account(recipient, self.node).balance,
            100_000,
        )


if __name__ == "__main__":
    unittest.main()
