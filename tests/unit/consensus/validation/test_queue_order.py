"""The worker drains the queue highest base fee first."""

import os
import sys
import unittest
from pathlib import Path
from queue import Empty, Queue
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from astreum.consensus.transaction import Transaction, TransactionCode
from astreum.consensus.validation.worker import (
    _apply_pending_transactions,
    _drain_pending_transactions,
)
from astreum.consensus.validation import worker as worker_module


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _signed(key: Ed25519PrivateKey, *, base_fee: int, amount: int = 5) -> Transaction:
    tx = Transaction(
        chain_id=0,
        amount=amount,
        code=TransactionCode.TRANSFER,
        counter=0,
        base_fee=base_fee,
        recipient=os.urandom(32),
        sender=_public(key),
    )
    tx.sign(key)
    return tx


def _node(*transactions):
    node = SimpleNamespace(
        _validation_transaction_queue=Queue(),
        logger=SimpleNamespace(
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        ),
    )
    for transaction in transactions:
        node._validation_transaction_queue.put(transaction)
    return node


class TestDrainPendingTransactions(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()

    def test_the_highest_bid_comes_first(self):
        txs = [_signed(self.key, base_fee=b) for b in (5, 1, 9, 3)]
        order = _drain_pending_transactions(_node(*txs))
        self.assertEqual([tx.base_fee for tx in order], [9, 5, 3, 1])

    def test_equal_bids_keep_arrival_order(self):
        first = _signed(self.key, base_fee=5, amount=1)
        second = _signed(self.key, base_fee=5, amount=2)
        order = _drain_pending_transactions(_node(first, second))
        self.assertEqual(order, [first, second])

    def test_a_zero_bid_is_dropped(self):
        bad = _signed(self.key, base_fee=0)
        good = _signed(self.key, base_fee=1)
        order = _drain_pending_transactions(_node(bad, good))
        self.assertEqual(order, [good])

    def test_an_empty_queue_drains_empty(self):
        self.assertEqual(_drain_pending_transactions(_node()), [])


class TestApplyPendingTransactions(unittest.TestCase):
    def test_an_unsupported_transaction_requeues_it_and_the_tail(self):
        first = SimpleNamespace(hash=b"\x01" * 32)
        second = SimpleNamespace(hash=b"\x02" * 32)
        third = SimpleNamespace(hash=b"\x03" * 32)
        applied = []

        def apply(node, block, transaction):
            applied.append(transaction)
            if transaction is second:
                raise NotImplementedError

        node = _node()
        with mock.patch.object(
            worker_module, "apply_transaction_obj", side_effect=apply
        ), mock.patch.object(worker_module.time, "sleep"):
            _apply_pending_transactions(node, None, [first, second, third])

        requeued = []
        while True:
            try:
                requeued.append(node._validation_transaction_queue.get_nowait())
            except Empty:
                break
        self.assertEqual(requeued, [second, third])
        self.assertEqual(applied, [first, second])


if __name__ == "__main__":
    unittest.main()
