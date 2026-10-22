"""Transactions that must not succeed, and bursts that must all succeed."""

import os
import unittest

from astreum.consensus.models.receipt import STATUS_FAILED, STATUS_SUCCESS
from astreum.consensus.transaction import create_transaction
from astreum.consensus.transaction.code import TransactionCode

from tests.e2e.harness import (
    SLOT,
    account,
    balance,
    chain,
    find_tx_block,
    public,
    push_transaction,
    receipt,
    transfer,
    tx_hashes,
    wait_until,
)
from tests.e2e.scenarios.test_user_transfer import _UserTestCase


class TestTxRejects(_UserTestCase):
    def test_replayed_counter_does_not_pay_twice(self) -> None:
        first = transfer(self.u, self.wallet, 0, self.recipient, 1000)
        replay = transfer(self.u, self.wallet, 0, self.recipient, 1001)
        push_transaction(self.u, first)
        self.included(first)
        push_transaction(self.u, replay)
        wait_until(
            lambda: find_tx_block(self.v, replay.hash) is not None
            or self.v.latest_block.height >= self.included(first).height + 4,
            timeout=SLOT * 12,
            message="replay processed or 4 blocks passed",
            nodes=self.nodes,
        )
        block = find_tx_block(self.v, replay.hash)
        if block is not None:
            self.assertEqual(receipt(self.v, block, replay.hash).status, STATUS_FAILED)
        self.assertEqual(balance(self.v, self.recipient), 1000)

    def test_wrong_chain_id_is_not_included(self) -> None:
        wrong = create_transaction(
            chain_id=self.u.config.chain_id + 99,
            sender=public(self.wallet),
            counter=0,
            recipient=self.recipient,
            code=TransactionCode.TRANSFER,
            amount=7,
            secret_key=self.wallet,
        )
        wrong.hash = wrong.expr().hash()
        good = transfer(self.u, self.wallet, 0, self.recipient, 8)
        push_transaction(self.u, wrong)
        push_transaction(self.u, good)
        self.included(good)
        self.assertIsNone(find_tx_block(self.v, wrong.hash))
        self.assertEqual(balance(self.v, self.recipient), 8)

    def test_overspend_fails_and_moves_nothing(self) -> None:
        over = transfer(self.u, self.wallet, 0, self.recipient, self.FUNDS * 10)
        push_transaction(self.u, over)
        block = self.included(over)
        self.assertEqual(receipt(self.v, block, over.hash).status, STATUS_FAILED)
        self.assertEqual(balance(self.v, self.recipient, block), 0)

    def test_bad_signature_is_not_included(self) -> None:
        # A forged signature is dropped at ingress and in the worker queue.
        forged = transfer(self.u, self.wallet, 0, self.recipient, 9)
        forged.signature = b"\x00" * 64
        forged._expr = None  # transfer() cached the expr with the real signature
        forged.hash = forged.expr().hash()
        good = transfer(self.u, self.wallet, 0, self.recipient, 10)
        push_transaction(self.u, forged)
        push_transaction(self.u, good)
        self.included(good, timeout=SLOT * 15)
        self.assertIsNone(find_tx_block(self.v, forged.hash))


class TestTxBurst(_UserTestCase):
    WALLETS = 3
    PER_USER = 10

    def test_every_transaction_lands_once_and_in_counter_order(self) -> None:
        sent = []
        for counter in range(self.PER_USER):
            for wallet in self.wallets:
                tx = transfer(self.u, wallet, counter, os.urandom(32), 10 + counter)
                push_transaction(self.u, tx)
                sent.append((public(wallet), counter, tx))

        for _, _, tx in sent:
            self.included(tx, timeout=SLOT * 40)

        seen = []
        for block in chain(self.v):
            seen.extend(tx_hashes(self.v, block))
        self.assertEqual(len(seen), len(set(seen)), "a transaction landed twice")
        extra = set(seen) - {tx.hash for _, _, tx in sent}
        self.assertEqual(extra, set(), "transactions nobody sent landed in a block")
        self.assertEqual(len(seen), self.WALLETS * self.PER_USER)

        for sender, counter, tx in sent:
            block = find_tx_block(self.v, tx.hash)
            self.assertEqual(
                receipt(self.v, block, tx.hash).status,
                STATUS_SUCCESS,
                f"counter {counter} of {sender.hex()[:8]} failed",
            )
        for wallet in self.wallets:
            self.assertEqual(account(self.v, public(wallet)).counter, self.PER_USER)


if __name__ == "__main__":
    unittest.main()
