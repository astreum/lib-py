"""Transaction signature checks: the model, the worker queue, and block verify."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from queue import Empty, Queue
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from astreum.expression import bytes_, link_list_to_expr
from astreum.consensus.block.verify import transactions as verify_module
from astreum.consensus.block.verify.transactions import verify_block_transactions
from astreum.consensus.transaction import Transaction, TransactionCode
from astreum.consensus.validation.worker import _next_signed_transaction


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _signed(key: Ed25519PrivateKey, amount: int = 5, counter: int = 0) -> Transaction:
    tx = Transaction(
        chain_id=0,
        amount=amount,
        code=TransactionCode.TRANSFER,
        counter=counter,
        recipient=os.urandom(32),
        sender=_public(key),
    )
    tx.sign(key)
    return tx


class TestVerifySignature(unittest.TestCase):
    def setUp(self) -> None:
        self.key = Ed25519PrivateKey.generate()

    def test_signed_transaction_is_valid(self) -> None:
        self.assertTrue(_signed(self.key).verify_signature())

    def test_unsigned_transaction_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.signature = None
        self.assertFalse(tx.verify_signature())

    def test_zero_signature_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.signature = b"\x00" * 64
        self.assertFalse(tx.verify_signature())

    def test_short_signature_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.signature = tx.signature[:63]
        self.assertFalse(tx.verify_signature())

    def test_signature_by_another_key_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.signature = Ed25519PrivateKey.generate().sign(tx.body_hash)
        self.assertFalse(tx.verify_signature())

    def test_changed_amount_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.amount += 1
        self.assertFalse(tx.verify_signature())

    def test_changed_recipient_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.recipient = os.urandom(32)
        self.assertFalse(tx.verify_signature())

    def test_changed_data_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.data = bytes_(b"other")
        self.assertFalse(tx.verify_signature())

    def test_stale_body_hash_is_not_trusted(self) -> None:
        tx = _signed(self.key)
        old_hash = tx.body_hash
        tx.amount += 1
        tx.body_hash = old_hash
        self.assertFalse(tx.verify_signature())

    def test_sender_of_wrong_length_is_invalid(self) -> None:
        tx = _signed(self.key)
        tx.sender = tx.sender[:31]
        self.assertFalse(tx.verify_signature())

    def test_signature_survives_the_expr_round_trip(self) -> None:
        from astreum.consensus.transaction.from_storage import get_transaction_from_storage
        from astreum.expression import resolve_inner_exprs
        from astreum.storage.exprs import put_expr_in_hot_storage
        from tests.unit.consensus.transaction.test_apply import _FakeNode

        node = _FakeNode()
        tx = _signed(self.key)
        exprs, missed = resolve_inner_exprs(node, tx.expr())
        self.assertEqual(missed, [])
        for expr in exprs:
            put_expr_in_hot_storage(node, expr)
        loaded = get_transaction_from_storage(node, tx.expr().hash())
        self.assertTrue(loaded.verify_signature())


class TestWorkerQueue(unittest.TestCase):
    def test_invalid_transactions_are_dropped_from_the_queue(self) -> None:
        key = Ed25519PrivateKey.generate()
        forged = _signed(key, amount=9)
        forged.signature = b"\x00" * 64
        good = _signed(key, amount=10)
        node = SimpleNamespace(
            _validation_transaction_queue=Queue(),
            logger=mock.Mock(),
        )
        node._validation_transaction_queue.put(forged)
        node._validation_transaction_queue.put(good)

        self.assertIs(_next_signed_transaction(node), good)
        with self.assertRaises(Empty):
            _next_signed_transaction(node)

    def test_queue_of_only_invalid_transactions_reads_as_empty(self) -> None:
        forged = _signed(Ed25519PrivateKey.generate())
        forged.signature = None
        node = SimpleNamespace(
            _validation_transaction_queue=Queue(),
            logger=mock.Mock(),
        )
        node._validation_transaction_queue.put(forged)
        with self.assertRaises(Empty):
            _next_signed_transaction(node)


class TestBlockVerify(unittest.TestCase):
    """A block that holds a forged transaction is rejected."""

    def _run(self, transactions: list[Transaction]) -> tuple[tuple, mock.Mock]:
        tx_hashes = [os.urandom(32) for _ in transactions]
        by_hash = dict(zip(tx_hashes, transactions))
        head = link_list_to_expr(tx_hashes).hash()
        validator = os.urandom(32)

        prev_block = SimpleNamespace(
            accounts_hash=os.urandom(32),
            bloom_hash=None,
            expr_id=os.urandom(32),
            height=3,
        )
        block = SimpleNamespace(
            expr_id=os.urandom(32),
            transactions_hash=head,
            receipts_hash=os.urandom(32),
            accounts_hash=os.urandom(32),
            previous_block_hash=prev_block.expr_id,
            previous_block=prev_block,
            validator_public_key_bytes=validator,
            chain_id=0,
            height=4,
            timestamp=0,
            difficulty=1,
            contact=None,
        )
        # A list read from storage ends in None, not in NIL.
        stored_list = link_list_to_expr(tx_hashes)
        cell = stored_list
        while cell.tail is not None and cell.tail.head_hash is not None:
            cell = cell.tail
        cell.tail = None
        accounts = mock.Mock()
        accounts.get_account.return_value = None
        work_block = SimpleNamespace(accounts=accounts, bloom_tree=None)
        node = SimpleNamespace(logger=mock.Mock())

        apply = mock.Mock()
        with mock.patch.object(verify_module, "get_expr_list", return_value=stored_list), \
             mock.patch.object(verify_module, "current_validator", return_value=(validator, accounts)), \
             mock.patch.object(verify_module, "create_block", return_value=work_block), \
             mock.patch.object(verify_module, "BloomTree"), \
             mock.patch.object(verify_module, "prefetch_exprs_from_network"), \
             mock.patch.object(
                 verify_module,
                 "get_transaction_from_storage",
                 side_effect=lambda _node, tx_hash, **_kw: by_hash[tx_hash],
             ), \
             mock.patch.object(verify_module, "apply_transaction_obj", apply), \
             mock.patch.object(verify_module, "get_block_expr"):
            try:
                result = verify_block_transactions(node, block)
            except Exception:
                # Past the signature loop the mocks end. The loop is what counts.
                result = None
        return result, apply

    def test_block_with_forged_transaction_is_rejected(self) -> None:
        key = Ed25519PrivateKey.generate()
        forged = _signed(key)
        forged.signature = b"\x00" * 64
        result, apply = self._run([forged])
        self.assertIsNotNone(result)
        ok, reason = result
        self.assertFalse(ok)
        self.assertIn("invalid transaction signature", reason)
        apply.assert_not_called()

    def test_forged_transaction_after_a_good_one_is_rejected(self) -> None:
        key = Ed25519PrivateKey.generate()
        forged = _signed(key, counter=1)
        forged.signature = b"\x00" * 64
        result, apply = self._run([_signed(key), forged])
        self.assertEqual(result[0], False)
        self.assertEqual(apply.call_count, 1)


if __name__ == "__main__":
    unittest.main()
