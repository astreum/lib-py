"""A temporary user sends transactions to a running validator."""

import os
import unittest

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    account,
    balance,
    block_at,
    find_tx_block,
    genesis_with,
    height,
    knows_validator,
    new_key,
    public,
    push_transaction,
    receipt,
    transfer,
    wait_until,
)
from astreum.consensus.models.receipt import STATUS_SUCCESS


class _UserTestCase(unittest.TestCase):
    """A validator V with funded wallets, and a user node U that knows V."""

    FUNDS = 1_000_000
    WALLETS = 1

    def setUp(self) -> None:
        self.net = Network()
        self.addCleanup(self.net.close)
        self.v_key = new_key()
        self.wallets = [new_key() for _ in range(self.WALLETS)]
        self.wallet = self.wallets[0]
        self.recipient = os.urandom(32)
        self.genesis = genesis_with(
            stakes={public(self.v_key): STAKE},
            funds={public(w): self.FUNDS for w in self.wallets},
        )
        self.v = self.net.validator(self.v_key, genesis=self.genesis)
        self.u = self.net.user(self.v, self.genesis)
        self.nodes = {"validator": self.v, "user": self.u}
        wait_until(
            lambda: height(self.v) >= 2 and height(self.u) >= 2 and knows_validator(self.u),
            timeout=SLOT * 15,
            message="user connected to the validator",
            nodes=self.nodes,
        )

    def included(self, tx, timeout=SLOT * 10):
        return wait_until(
            lambda: find_tx_block(self.v, tx.hash),
            timeout=timeout,
            message="transaction in a block",
            nodes=self.nodes,
        )


class TestUserTransfer(_UserTestCase):
    def test_transfer_reaches_a_block_with_a_success_receipt(self) -> None:
        sent_before = balance(self.v, public(self.wallet))
        tx = transfer(self.u, self.wallet, 0, self.recipient, 1000)
        self.assertGreaterEqual(push_transaction(self.u, tx), 1)

        block = self.included(tx)
        self.assertEqual(receipt(self.v, block, tx.hash).status, STATUS_SUCCESS)
        self.assertEqual(balance(self.v, self.recipient, block), 1000)
        fee = sent_before - 1000 - balance(self.v, public(self.wallet), block)
        self.assertGreater(fee, 0, "the sender pays a fee on top of the amount")
        self.assertEqual(account(self.v, public(self.wallet), block).counter, 1)

    def test_user_node_sees_the_same_block(self) -> None:
        tx = transfer(self.u, self.wallet, 0, self.recipient, 5)
        push_transaction(self.u, tx)
        block = self.included(tx)
        wait_until(
            lambda: height(self.u) >= block.height + 1,
            timeout=SLOT * 10,
            message="user node past the block",
            nodes=self.nodes,
        )
        self.assertEqual(
            block_at(self.u, block.height).expr_id,
            block.expr_id,
            "user node holds a different block at the inclusion height",
        )


class TestUserLeaves(_UserTestCase):
    def test_transaction_lands_after_the_user_disconnects(self) -> None:
        tx = transfer(self.u, self.wallet, 0, self.recipient, 77)
        push_transaction(self.u, tx)
        wait_until(
            lambda: self.u.outgoing_queue.empty(),
            timeout=SLOT * 3,
            message="user outgoing queue empty",
            nodes=self.nodes,
        )
        self.net.stop(self.u)

        block = self.included(tx)
        self.assertEqual(balance(self.v, self.recipient, block), 77)


if __name__ == "__main__":
    unittest.main()
