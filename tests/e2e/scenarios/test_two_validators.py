"""Two validators that both hold stake."""

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


class TestTwoValidators(unittest.TestCase):
    def test_both_validators_sign_and_agree(self) -> None:
        key_a, key_b = new_key(), new_key()
        genesis = genesis_with(stakes={public(key_a): STAKE, public(key_b): STAKE})
        with Network() as net:
            a = net.validator(key_a, genesis=genesis)
            b = net.validator(key_b, genesis=genesis, seed=a)
            nodes = {"a": a, "b": b}

            def signers():
                return {blk.validator_public_key_bytes for blk in assert_chain_links(self, a)[1:]}

            # With two stakes the scheduler skips slots until it draws a
            # validator that is online, so blocks come every 10 to 30 s.
            wait_until(
                lambda: height(a) >= 3 and height(b) >= 3,
                timeout=SLOT * 40,
                message="both validators at height 3",
                nodes=nodes,
            )
            wait_until(
                lambda: len(signers()) == 2,
                timeout=SLOT * 80,
                message="blocks signed by both validators",
                nodes=nodes,
            )

            # Same chain on both nodes at every height both have.
            shared = min(height(a), height(b)) - 1
            self.assertGreaterEqual(shared, 2)
            for at in range(shared + 1):
                self.assertEqual(
                    block_at(a, at).expr_id,
                    block_at(b, at).expr_id,
                    f"a and b disagree at height {at}",
                )
            self.assertEqual(signers(), {public(key_a), public(key_b)})


if __name__ == "__main__":
    unittest.main()
