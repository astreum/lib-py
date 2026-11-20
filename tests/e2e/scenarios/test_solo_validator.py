"""A single validator on its own."""

import unittest

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


class TestSoloValidator(unittest.TestCase):
    def test_chain_grows_without_gaps(self) -> None:
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        with Network() as net:
            node = net.validator(key, genesis=genesis)
            wait_until(
                lambda: height(node) >= 5,
                timeout=SLOT * 10,
                message="height 5",
                nodes=[node],
            )
            blocks = assert_chain_links(self, node, signers={public(key)})

        self.assertEqual(blocks[0].height, 0)
        self.assertGreaterEqual(blocks[-1].height, 5)

    def test_average_block_time_is_under_four_seconds(self) -> None:
        key = new_key()
        genesis = genesis_with(stakes={public(key): STAKE})
        with Network() as net:
            node = net.validator(key, genesis=genesis)
            wait_until(
                lambda: height(node) >= 6,
                timeout=SLOT * 12,
                message="height 6",
                nodes=[node],
            )
            blocks = assert_chain_links(self, node)

        # The gap from genesis to block 1 includes start-up, so it is skipped.
        # One gap can reach 6 s under load. The average is the stable value.
        gaps = [b.timestamp - a.timestamp for a, b in zip(blocks[1:], blocks[2:])]
        self.assertGreaterEqual(len(gaps), 4)
        self.assertGreaterEqual(min(gaps), 2)
        self.assertLessEqual(max(gaps), 10)
        self.assertLessEqual(sum(gaps) / len(gaps), 4)


if __name__ == "__main__":
    unittest.main()
