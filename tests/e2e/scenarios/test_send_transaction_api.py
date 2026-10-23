"""The public ``send_transaction`` call."""

import os
import unittest

from astreum.consensus.transaction.send import send_transaction

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    find_tx_block,
    genesis_with,
    height,
    knows_validator,
    new_key,
    public,
    transfer,
    wait_until,
)


class TestSendTransactionApi(unittest.TestCase):
    def test_send_transaction_reaches_the_validator(self) -> None:
        # The user node holds no validation key. It learns the validator
        # from pings, and ``send_transaction`` sends the transaction to it.
        v_key, wallet = new_key(), new_key()
        genesis = genesis_with(
            stakes={public(v_key): STAKE}, funds={public(wallet): 1_000_000}
        )
        with Network() as net:
            validator = net.validator(v_key, genesis=genesis)
            user = net.user(validator, genesis)
            nodes = {"validator": validator, "user": user}
            wait_until(
                lambda: height(validator) >= 2 and height(user) >= 2 and knows_validator(user),
                timeout=SLOT * 15,
                message="user connected",
                nodes=nodes,
            )
            tx = transfer(user, wallet, 0, os.urandom(32), 10)
            send_transaction(user, tx)
            wait_until(
                lambda: find_tx_block(validator, tx.hash),
                timeout=SLOT * 10,
                message="transaction in a block",
                nodes=nodes,
            )


if __name__ == "__main__":
    unittest.main()
