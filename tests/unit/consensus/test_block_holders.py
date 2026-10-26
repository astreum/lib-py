"""A relay key change updates the block holders that carry the old key."""
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, "src")

from astreum.communication.models.relay_endpoint import RelayEndpoint
from astreum.consensus.block.holders import (
    block_holder,
    init_block_holders,
    record_block_holder,
    replace_holder_relay_key,
)

OLD = b"\x01" * 32
NEW = b"\x02" * 32
OTHER = b"\x03" * 32


def _node():
    node = SimpleNamespace()
    init_block_holders(node)
    return node


class TestReplaceHolderRelayKey(unittest.TestCase):
    def setUp(self):
        self.node = _node()
        record_block_holder(self.node, b"\x10" * 32, RelayEndpoint(OLD, ("10.0.0.1", 1)))
        record_block_holder(self.node, b"\x11" * 32, RelayEndpoint(OTHER, ("10.0.0.2", 2)))
        record_block_holder(self.node, b"\x12" * 32, RelayEndpoint(OLD, ("10.0.0.3", 3)))

    def test_replace_changes_matching_entries(self):
        replace_holder_relay_key(self.node, OLD, NEW)
        self.assertEqual(block_holder(self.node, b"\x10" * 32).relay_key, NEW)
        self.assertEqual(block_holder(self.node, b"\x12" * 32).relay_key, NEW)
        self.assertEqual(block_holder(self.node, b"\x11" * 32).relay_key, OTHER)

    def test_replace_keeps_address_and_order(self):
        order = list(self.node.block_holders)
        replace_holder_relay_key(self.node, OLD, NEW)
        self.assertEqual(list(self.node.block_holders), order)
        self.assertEqual(block_holder(self.node, b"\x10" * 32).address, ("10.0.0.1", 1))
        self.assertEqual(block_holder(self.node, b"\x12" * 32).address, ("10.0.0.3", 3))

    def test_replace_returns_the_count(self):
        self.assertEqual(replace_holder_relay_key(self.node, OLD, NEW), 2)

    def test_replace_does_not_change_the_old_object(self):
        before = block_holder(self.node, b"\x10" * 32)
        replace_holder_relay_key(self.node, OLD, NEW)
        self.assertEqual(before.relay_key, OLD)

    def test_replace_with_no_match(self):
        self.assertEqual(replace_holder_relay_key(self.node, b"\x09" * 32, NEW), 0)
        self.assertEqual(block_holder(self.node, b"\x10" * 32).relay_key, OLD)
        self.assertEqual(block_holder(self.node, b"\x11" * 32).relay_key, OTHER)

    def test_replace_without_a_table(self):
        self.assertEqual(replace_holder_relay_key(SimpleNamespace(), OLD, NEW), 0)

    def test_the_first_holder_is_still_kept(self):
        replace_holder_relay_key(self.node, OLD, NEW)
        self.assertFalse(
            record_block_holder(self.node, b"\x10" * 32, RelayEndpoint(OTHER, ("10.0.0.9", 9)))
        )
        self.assertEqual(block_holder(self.node, b"\x10" * 32).relay_key, NEW)


if __name__ == "__main__":
    unittest.main()
