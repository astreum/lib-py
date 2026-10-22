"""A validator with one long-term peer."""

import unittest

from tests.e2e.harness import (
    SLOT,
    STAKE,
    Network,
    assert_chain_links,
    block_at,
    genesis_with,
    height,
    new_key,
    public,
    wait_until,
)


def _lag(validator, peer) -> int:
    return height(validator) - height(peer)


class TestValidatorAndPeer(unittest.TestCase):
    def test_unstaked_validator_peer_follows_the_chain(self) -> None:
        """A node with a validation key but no stake follows through BLOCK announcements."""
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        with Network() as net:
            validator = net.validator(key, genesis=genesis)
            peer = net.user(validator, genesis)
            nodes = {"validator": validator, "peer": peer}

            wait_until(
                lambda: height(peer) >= 3,
                timeout=SLOT * 15,
                message="peer height 3",
                nodes=nodes,
            )
            # Three checks in a row, a slot apart.
            for _ in range(3):
                wait_until(
                    lambda: 0 <= _lag(validator, peer) <= 2,
                    timeout=SLOT * 6,
                    message="peer within 2 blocks of the validator",
                    nodes=nodes,
                )
                wait_until(
                    lambda h=height(peer): height(peer) > h,
                    timeout=SLOT * 4,
                    message="peer advances",
                    nodes=nodes,
                )

            shared = min(height(validator), height(peer)) - 1
            self.assertGreaterEqual(shared, 3)
            for at in range(shared + 1):
                self.assertEqual(
                    block_at(peer, at).expr_id,
                    block_at(validator, at).expr_id,
                    f"peer and validator disagree at height {at}",
                )
            assert_chain_links(self, peer, signers={public(key)})


class TestNonValidatorPeer(unittest.TestCase):
    """A node with no validation key.

    BUG (all tests here): BLOCK announcements go only to validators
    (validation/worker.py ``_announce_block``), the ping handler records no
    block holder, and ``get_block_from_storage`` without a contact does not go
    to the network. So a non-validator cannot load the head block.
    """

    def _validator(self, net):
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        validator = net.validator(key, genesis=genesis)
        wait_until(
            lambda: height(validator) >= 2,
            timeout=SLOT * 6,
            message="validator height 2",
            nodes=[validator],
        )
        return validator

    @unittest.expectedFailure
    def test_verifying_peer_follows_the_validator(self) -> None:
        with Network() as net:
            validator = self._validator(net)
            peer = net.peer(validator, trusted=False)
            wait_until(
                lambda: height(peer) >= 2,
                timeout=SLOT * 15,
                message="verifying peer has a head block",
                nodes={"validator": validator, "peer": peer},
            )

    @unittest.expectedFailure
    def test_trusted_peer_has_a_head_block(self) -> None:
        # The ping handler sets ``latest_block = None`` on a new head hash and
        # nothing loads the block again.
        with Network() as net:
            validator = self._validator(net)
            peer = net.peer(validator, trusted=True)
            wait_until(
                lambda: height(peer) >= 2,
                timeout=SLOT * 15,
                message="trusted peer has a head block",
                nodes={"validator": validator, "peer": peer},
            )


if __name__ == "__main__":
    unittest.main()
