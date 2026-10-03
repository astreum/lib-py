"""nil is referenced as ZERO32 everywhere, so hashing nil gives ZERO32."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.models.receipt import Receipt  # noqa: E402
from astreum.expression import (  # noqa: E402
    NIL,
    ZERO32,
    bytes_,
    exprs_to_linked_expr,
    link,
    link_list_to_expr,
)


class TestNilHash(unittest.TestCase):
    def test_nil_hashes_to_zero32(self) -> None:
        self.assertEqual(NIL.hash(), ZERO32)

    def test_empty_lists_hash_to_zero32(self) -> None:
        self.assertEqual(link_list_to_expr([]).hash(), ZERO32)
        self.assertEqual(exprs_to_linked_expr([]).hash(), ZERO32)

    def test_nil_node_is_not_nil(self) -> None:
        # Only the NIL singleton is nil; a link with nil children is a real node.
        self.assertNotEqual(link(NIL, NIL).hash(), ZERO32)

    def test_nil_child_still_hashes_as_zero32(self) -> None:
        # Unchanged: a nil child contributes ZERO32 to its parent's hash.
        tail = bytes_(b"x")
        parent = link(NIL, tail)
        same = link(None, tail)
        self.assertEqual(parent.hash(), same.hash())

    def test_receipt_without_logs_uses_zero32(self) -> None:
        tx = b"\x01" * 32
        implicit = Receipt(tx, 1, 0, 0, 0, 0)
        explicit = Receipt(tx, 1, 0, 0, 0, 0, logs_hash=NIL.hash())
        self.assertEqual(explicit.logs_hash, ZERO32)
        self.assertEqual(explicit.expr().hash(), implicit.expr().hash())


if __name__ == "__main__":
    unittest.main()
