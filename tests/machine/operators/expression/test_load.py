import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression import NIL, ZERO32, int_, bytes_, symbol, link
from tests.machine.operators.expression.test_ref import (
    BLOCK,
    BLOCK_HEX,
    ZERO_HEX,
    FakeNode,
    _is_nil,
    _is_tagged,
    machine_with_blocks,
    stored_ref,
    store,
)


def _load_expr(hash_bytes: bytes, block: bytes = BLOCK) -> Expr:
    """``(block_id expr_id load)``."""
    return link(bytes_(block), link(bytes_(hash_bytes), link(symbol("load"), None)))


class TestLoadOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_load_empty_stack_returns_nil(self):
        expr, _ = parse(tokenize("(load)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_load_one_argument_is_an_underflow(self):
        expr, _ = parse(tokenize(f"(0x{'01' * 32} load?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result._head.value, "stack underflow")

    def test_load_zero32_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{ZERO_HEX} load)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_load_non_bytes_returns_nil(self):
        expr, _ = parse(tokenize(f'(0x{BLOCK_HEX} "not-a-hash" load)'))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_load_short_bytes_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0xdead load)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_load_no_node_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{'01' * 32} load)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_load_resolves_symbol(self):
        stored = symbol("hello")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        result = Machine(node=node).run(expr=_load_expr(stored.hash()))
        self.assertEqual(result._tag, "symbol")
        self.assertEqual(result.value, "hello")

    def test_load_resolves_link_concrete(self):
        stored = link(bytes_(b"\x01"), bytes_(b"\x02"))
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        result = Machine(node=node).run(expr=_load_expr(stored.hash()))
        self.assertEqual(result._tag, "link")
        self.assertEqual(result._head.value, b"\x01")
        self.assertEqual(result._tail.value, b"\x02")

    def test_load_resolves_hash_children(self):
        head, tail = bytes_(b"head"), bytes_(b"tail")
        cell = Expr("link", head_hash=head.hash(), tail_hash=tail.hash())
        node = FakeNode()
        for e in (head, tail, cell):
            node.hot_storage[e.hash()] = e
        result = Machine(node=node).run(expr=_load_expr(cell.hash()))
        self.assertEqual(result._head.value, b"head")
        self.assertEqual(result._tail.value, b"tail")
        # The shared cell in storage was not changed.
        self.assertIsNone(cell._head)

    def test_load_meter_double_size(self):
        stored = link(bytes_(b"\x01"), bytes_(b"\x02"))
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        machine = Machine(node=node, meter_limit=10_000)
        machine.run(expr=_load_expr(stored.hash()))
        self.assertEqual(machine.meter.total, stored.size() * 2 + 32 + 32)

    def test_load_deterministic(self):
        stored = bytes_(b"\x01\x02\x03")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        result = Machine(node=node, mode="deterministic").run(expr=_load_expr(stored.hash()))
        self.assertEqual(result.value, b"\x01\x02\x03")

    def test_load_zero32_ok(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{ZERO_HEX} load?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertTrue(_is_nil(result._head))

    def test_load_non_bytes_err(self):
        expr, _ = parse(tokenize(f'(0x{BLOCK_HEX} "not-a-hash" load?)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(result._head.value, "load requires 32-byte hash, got str")

    def test_load_short_bytes_err(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0xdead load?)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result._head.value, "load requires 32-byte hash, got 2 bytes")

    def test_load_no_node_err(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{'01' * 32} load?)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result._head.value, "load requires a node connection")

    def test_load_unknown_block_err(self):
        node = FakeNode()
        stored = symbol("x")
        store(node, stored)
        machine = machine_with_blocks(node, b"\xb2" * 32)
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{stored.hash().hex()} load?)"))
        result = machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result._head.value, "load: unknown block")


class TestLoadFollowsRefs(unittest.TestCase):
    """A trie written over several blocks: older nodes are stored refs."""

    OLD = b"\xc3" * 32

    def test_every_ref_below_the_root_is_followed(self):
        old_leaf = link(bytes_(b"old-a"), bytes_(b"old-b"))
        node = FakeNode()
        store(node, old_leaf)
        pointer = stored_ref(self.OLD, old_leaf.hash())
        new_value = bytes_(b"new")
        root = link(link(new_value, pointer), link(int_(7), NIL))
        store(node, root)

        machine = machine_with_blocks(node, BLOCK, self.OLD)
        result = machine.run(expr=_load_expr(root.hash()))

        # (new-value . old-leaf) with the pointer replaced by what it names.
        self.assertEqual(result._head._head.value, b"new")
        self.assertEqual(result._head._tail.hash(), old_leaf.hash())

    def test_a_ref_to_a_ref_is_returned_as_data(self):
        leaf = bytes_(b"leaf")
        node = FakeNode()
        store(node, leaf)
        inner = stored_ref(self.OLD, leaf.hash())
        store(node, inner)
        outer = stored_ref(self.OLD, inner.hash())
        store(node, outer)
        machine = machine_with_blocks(node, BLOCK, self.OLD)
        result = machine.run(expr=_load_expr(outer.hash()))
        self.assertEqual(result.hash(), inner.hash())

    def test_a_block_a_ref_names_must_be_known(self):
        leaf = bytes_(b"leaf")
        node = FakeNode()
        store(node, leaf)
        pointer = stored_ref(self.OLD, leaf.hash())
        root = link(bytes_(b"k"), pointer)
        store(node, root)
        machine = machine_with_blocks(node, BLOCK)  # OLD unknown
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{root.hash().hex()} load?)"))
        result = machine.run(expr=expr)
        self.assertEqual(result._head.value, "load: unknown block")


if __name__ == "__main__":
    unittest.main(verbosity=2)
