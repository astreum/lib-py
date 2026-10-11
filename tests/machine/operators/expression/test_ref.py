import sys
import unittest
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.models.accounts import Accounts
from astreum.expression import NIL, ZERO32, int_, fp64_, bytes_, str_, symbol, link
from astreum.storage.radix import RadixTree, put_in_radix_tree
from astreum.consensus.account import create_account
from astreum.expression.helpers import get_expr_builtin_tag

BLOCK = b"\xb1" * 32
BLOCK_HEX = BLOCK.hex()
ZERO_HEX = "00" * 32


def _is_tagged(expr, tag):
    return (
        get_expr_builtin_tag(expr) == "link"
        and expr.tail is not None
        and get_expr_builtin_tag(expr.tail) == "symbol"
        and expr.tail.value == tag
    )


def _is_nil(result):
    return get_expr_builtin_tag(result) == "link" and result.head is None and result.tail is None


class FakeNode:
    def __init__(self):
        self.hot_storage = {}
        self.hot_storage_timestamps = {}
        self.hot_storage_lock = threading.Lock()
        self.config = {"expr_fetch_interval": 0, "expr_fetch_retries": 0}
        self.logger = type(
            "L", (), {"debug": lambda *a, **kw: None, "info": lambda *a, **kw: None}
        )()

    def get_expr(self, expr_id: bytes):
        return self.hot_storage.get(expr_id)


def _ref_expr(hash_bytes: bytes, block: bytes = BLOCK) -> Expr:
    """``(block_id expr_id ref)``."""
    return link(bytes_(block), link(bytes_(hash_bytes), symbol("ref")))


def stored_ref(block: bytes, expr_id: bytes) -> Expr:
    """The expression the system writes for an older cell."""
    return link(bytes_(block), link(bytes_(expr_id), link(symbol("ref"), NIL)))


def store(node, expr):
    """Store *expr* and everything below it, each cell on its own."""
    stack = [expr]
    while stack:
        e = stack.pop()
        node.hot_storage[e.hash()] = e
        if get_expr_builtin_tag(e) == "link":
            stack.extend(c for c in (e.head, e.tail) if c is not None)


def machine_with_blocks(node, *blocks):
    """A machine whose block's storage trie holds *blocks* as keys."""
    storage = create_account()
    storage.data = RadixTree(root_hash=None)
    for b in blocks:
        put_in_radix_tree(storage.data, node, b, int_(1))
    accounts = Accounts()
    accounts.set_account(STORAGE_ADDRESS, storage)
    machine = Machine(node=node)
    machine.block = type("B", (), {"accounts": accounts})()
    return machine


class TestRefOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_ref_empty_stack_returns_nil(self):
        expr, _ = parse(tokenize("(ref)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_ref_one_argument_is_an_underflow(self):
        expr, _ = parse(tokenize(f"(0x{'01' * 32} ref?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")

    def test_ref_zero32_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{ZERO_HEX} ref)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_ref_non_bytes_returns_nil(self):
        expr, _ = parse(tokenize(f'(0x{BLOCK_HEX} "not-a-hash" ref)'))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_ref_short_bytes_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0xdead ref)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_ref_no_node_returns_nil(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{'01' * 32} ref)"))
        self.assertTrue(_is_nil(self.machine.run(expr=expr)))

    def test_ref_resolves_symbol(self):
        stored = symbol("hello")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        result = Machine(node=node).run(expr=_ref_expr(stored.hash()))
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "hello")

    def test_ref_asks_the_block_for_the_expr(self):
        stored = symbol("hello")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        with mock.patch(
            "astreum.machine.operators.expression._scoped.get_expr", return_value=stored
        ) as get:
            Machine(node=node).run(expr=_ref_expr(stored.hash()))
        get.assert_called_once_with(node, stored.hash(), block_hash=BLOCK)

    def test_ref_meter_link_fixed_70(self):
        stored = link(bytes_(b"\x01"), bytes_(b"\x02"))
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        machine = Machine(node=node, meter_limit=10_000)
        machine.run(expr=_ref_expr(stored.hash()))
        # block id + expr id evaluated (32 each) + the link result;
        # the cell is not a ref, and its parts are loaded: nothing fetched.
        self.assertEqual(machine.meter.total, 70 + 32 + 32)

    def test_ref_meter_bytes_sized(self):
        stored = bytes_(b"\x01\x02\x03")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        machine = Machine(node=node, meter_limit=10_000)
        machine.run(expr=_ref_expr(stored.hash()))
        self.assertEqual(machine.meter.total, stored.size() + 32 + 32)

    def test_ref_deterministic(self):
        stored = bytes_(b"\x01\x02\x03")
        node = FakeNode()
        node.hot_storage[stored.hash()] = stored
        result = Machine(node=node, mode="deterministic").run(expr=_ref_expr(stored.hash()))
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x01\x02\x03")

    def test_ref_zero32_ok(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{ZERO_HEX} ref?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertTrue(_is_nil(result.head))

    def test_ref_non_bytes_err(self):
        expr, _ = parse(tokenize(f'(0x{BLOCK_HEX} "not-a-hash" ref?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "ref requires 32-byte hash, got str")

    def test_ref_short_bytes_err(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0xdead ref?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "ref requires 32-byte hash, got 2 bytes")

    def test_ref_bad_block_err(self):
        expr, _ = parse(tokenize(f"(0xdead 0x{'01' * 32} ref?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "ref requires 32-byte block id, got 2 bytes")

    def test_ref_no_node_err(self):
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{'01' * 32} ref?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "ref requires a node connection")


class TestRefBlocks(unittest.TestCase):
    """The block must be one the executing block's storage trie knows."""

    def setUp(self):
        self.node = FakeNode()
        self.stored = symbol("inner")
        store(self.node, self.stored)

    def _run(self, machine, expr_text):
        expr, _ = parse(tokenize(expr_text))
        return machine.run(expr=expr)

    def test_known_block_resolves(self):
        machine = machine_with_blocks(self.node, BLOCK)
        result = machine.run(expr=_ref_expr(self.stored.hash()))
        self.assertEqual(result.value, "inner")

    def test_unknown_block_fails_the_same_for_two_validators(self):
        errors = []
        for _ in range(2):
            machine = machine_with_blocks(self.node, b"\xb2" * 32)
            expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{self.stored.hash().hex()} ref?)"))
            errors.append(machine.run(expr=expr))
        for result in errors:
            self.assertTrue(_is_tagged(result, "err"))
            self.assertEqual(result.head.value, "ref: unknown block")

    def test_a_block_header_is_read_as_its_own_block(self):
        header = link(bytes_(b"h"), link(bytes_(b"e"), NIL))
        store(self.node, header)
        machine = machine_with_blocks(self.node, header.hash())
        result = machine.run(expr=_ref_expr(header.hash(), block=header.hash()))
        self.assertEqual(get_expr_builtin_tag(result), "link")


class TestRefThunks(unittest.TestCase):
    def test_children_carry_the_same_block(self):
        tail = bytes_(b"tail")
        head = bytes_(b"head")
        cell = Expr("link", head_hash=head.hash(), tail_hash=tail.hash())
        node = FakeNode()
        store(node, head)
        store(node, tail)
        node.hot_storage[cell.hash()] = cell
        result = Machine(node=node).run(expr=_ref_expr(cell.hash()))
        self.assertEqual(get_expr_builtin_tag(result), "link")
        for thunk, child in ((result.head, head), (result.tail, tail)):
            self.assertEqual(thunk.head.value, BLOCK)
            self.assertEqual(thunk.tail.head.value, child.hash())
            self.assertEqual(thunk.tail.tail.value, "ref")

    def test_thunk_evaluates_to_the_child(self):
        tail = bytes_(b"tail")
        head = bytes_(b"head")
        cell = Expr("link", head_hash=head.hash(), tail_hash=tail.hash())
        node = FakeNode()
        for e in (head, tail, cell):
            node.hot_storage[e.hash()] = e
        pair = Machine(node=node).run(expr=_ref_expr(cell.hash()))
        again = Machine(node=node).run(expr=pair.head)
        self.assertEqual(again.value, b"head")


class TestStoredRefs(unittest.TestCase):
    """A fetched cell that is itself ``(block_id expr_id ref)`` is followed."""

    OLD = b"\xc3" * 32

    def setUp(self):
        self.node = FakeNode()
        self.target_head = bytes_(b"old-head")
        self.target_tail = bytes_(b"old-tail")
        self.target = link(self.target_head, self.target_tail)
        store(self.node, self.target)
        self.pointer = stored_ref(self.OLD, self.target.hash())
        store(self.node, self.pointer)

    def test_ref_follows_one_level_and_thunks_carry_the_new_block(self):
        machine = machine_with_blocks(self.node, BLOCK, self.OLD)
        result = machine.run(expr=_ref_expr(self.pointer.hash()))
        self.assertEqual(result.head.head.value, self.OLD)
        self.assertEqual(result.head.tail.head.value, self.target_head.hash())
        self.assertEqual(result.tail.head.value, self.OLD)

    def test_the_new_block_is_checked_too(self):
        machine = machine_with_blocks(self.node, BLOCK)  # OLD is not a key
        expr, _ = parse(tokenize(f"(0x{BLOCK_HEX} 0x{self.pointer.hash().hex()} ref?)"))
        result = machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "ref: unknown block")

    def test_a_ref_to_a_ref_is_returned_as_data(self):
        outer = stored_ref(BLOCK, self.pointer.hash())
        store(self.node, outer)
        machine = machine_with_blocks(self.node, BLOCK, self.OLD)
        result = machine.run(expr=_ref_expr(outer.hash()))
        # Followed once: the cell of the inner ref, handed back as thunks over
        # its own children, not followed again.
        self.assertEqual(result.head.head.value, BLOCK)
        self.assertEqual(result.head.tail.head.value, self.pointer.head.hash())

    def test_an_ordinary_cell_is_not_mistaken_for_a_ref(self):
        ordinary = link(bytes_(b"x" * 32), link(bytes_(b"y" * 32), link(symbol("nope"), NIL)))
        store(self.node, ordinary)
        machine = machine_with_blocks(self.node, BLOCK)
        result = machine.run(expr=_ref_expr(ordinary.hash()))
        self.assertEqual(result.head.head.value, BLOCK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
