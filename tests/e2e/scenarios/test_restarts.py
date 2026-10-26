"""Restarts and late joins."""

import unittest

from astreum.communication.node import connect_node

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    assert_chain_links,
    genesis_with,
    height,
    new_key,
    public,
    wait_until,
)


class TestValidatorRestart(unittest.TestCase):
    def test_validator_resumes_from_its_stored_head(self) -> None:
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        with Network() as net:
            node = net.validator(key, genesis=genesis)
            wait_until(
                lambda: height(node) >= 3,
                timeout=SLOT * 10,
                message="height 3",
                nodes=[node],
            )
            net.stop(node)
            old_height = height(node)
            old_hash = node.latest_block_hash
            old_blocks = [b.expr_id for b in assert_chain_links(self, node)]

            connect_node(node)
            net.start_validation(node, key, genesis=genesis)
            self.assertEqual(node.latest_block_hash, old_hash, "resumed from another head")

            wait_until(
                lambda: height(node) > old_height,
                timeout=SLOT * 10,
                message="a block after the restart",
                nodes=[node],
            )
            blocks = assert_chain_links(self, node, signers={public(key)})
            self.assertEqual([b.expr_id for b in blocks[: old_height + 1]], old_blocks)


class TestJoinAndRejoin(unittest.TestCase):
    """BUG (both tests): a node that misses a block never catches up.

    ``handle_block`` drops a BLOCK that is not built on the node latest block.
    The verify worker needs a block holder for the head, and the holder is
    recorded only from a BLOCK that passed that check.
    """

    @unittest.expectedFailure
    def test_late_joiner_catches_up(self) -> None:
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        with Network() as net:
            validator = net.validator(key, genesis=genesis)
            wait_until(
                lambda: height(validator) >= 3,
                timeout=SLOT * 10,
                message="validator height 3",
                nodes=[validator],
            )
            late = net.user(validator, genesis)
            wait_until(
                lambda: height(late) >= height(validator) - 2 and height(late) >= 3,
                timeout=SLOT * 15,
                message="late joiner within 2 blocks",
                nodes={"validator": validator, "late": late},
            )


if __name__ == "__main__":
    unittest.main()
