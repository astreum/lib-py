"""One of two validators goes offline and comes back."""

import unittest

from astreum.communication.node import connect_node

from tests.e2e.harness import (
    SLOT,
    Network,
    assert_chain_links,
    balance,
    block_at,
    genesis_with,
    height,
    new_key,
    public,
    stake,
    wait_until,
)

# Each block halves the stake of the validator it draws. A large stake keeps
# the drop visible after many halvings.
BIG_STAKE = 1 << 20


class TestValidatorOffline(unittest.TestCase):
    def setUp(self) -> None:
        self.key_online, self.key_offline = new_key(), new_key()
        self.genesis = genesis_with(
            stakes={public(self.key_online): BIG_STAKE, public(self.key_offline): BIG_STAKE}
        )

    def start(self, net: Network):
        """Start both validators and a peer. Wait until all three are at height 3.

        The offline validator has the online validator as seed. It can find
        the network again after a restart.
        """
        online = net.validator(self.key_online, genesis=self.genesis)
        offline = net.validator(self.key_offline, genesis=self.genesis, seed=online)
        peer = net.user(online, self.genesis)
        nodes = {"online": online, "offline": offline, "peer": peer}
        wait_until(
            lambda: all(height(n) >= 3 for n in nodes.values()),
            timeout=SLOT * 40,
            message="all nodes at height 3",
            nodes=nodes,
        )
        return online, offline, peer, nodes

    def test_other_validator_keeps_the_chain_going(self) -> None:
        with Network() as net:
            online, offline, peer, nodes = self.start(net)
            net.stop(offline)
            start_height = height(online)

            target = start_height + 2
            wait_until(
                lambda: height(online) >= target,
                timeout=SLOT * 80,
                message="two blocks from the validator that stayed online",
                nodes=nodes,
            )
            blocks = assert_chain_links(self, online, signers=None)
            for block in blocks:
                if block.height > start_height:
                    self.assertEqual(block.validator_public_key_bytes, public(self.key_online))

            wait_until(
                lambda: height(peer) >= target,
                timeout=SLOT * 40,
                message="peer at the new height",
                nodes=nodes,
            )
            self.assertEqual(block_at(peer, target).expr_id, block_at(online, target).expr_id)

    def test_offline_validator_loses_stake_and_rejoins(self) -> None:
        with Network() as net:
            online, offline, peer, nodes = self.start(net)
            stake_before = stake(online, self.key_offline)
            balance_before = balance(online, public(self.key_offline))

            net.stop(offline)
            offline_height = height(offline)
            target = height(online) + 2
            wait_until(
                lambda: height(online) >= target,
                timeout=SLOT * 80,
                message="two blocks from the validator that stayed online",
                nodes=nodes,
            )
            self.assertLess(stake(online, self.key_offline), stake_before)
            self.assertGreater(balance(online, public(self.key_offline)), balance_before)

            connect_node(offline)
            net.start_validation(offline, self.key_offline, genesis=self.genesis)
            wait_until(
                lambda: height(offline) > offline_height and height(offline) >= height(online) - 1,
                timeout=SLOT * 80,
                message="offline validator caught up",
                nodes=nodes,
            )
            shared = min(height(online), height(offline))
            for at in range(shared + 1):
                self.assertEqual(
                    block_at(online, at).expr_id,
                    block_at(offline, at).expr_id,
                    f"validators disagree at height {at}",
                )

            restart_height = height(online)

            def offline_signed_again() -> bool:
                return any(
                    block.validator_public_key_bytes == public(self.key_offline)
                    for block in assert_chain_links(self, online)
                    if block.height > restart_height
                )

            wait_until(
                offline_signed_again,
                timeout=SLOT * 120,
                message="a block signed by the validator that came back",
                nodes=nodes,
            )


if __name__ == "__main__":
    unittest.main()
