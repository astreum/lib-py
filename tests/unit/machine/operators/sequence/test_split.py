import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression import NIL, int_, fp64_, bytes_, str_, symbol, link
from astreum.expression.helpers import get_expr_builtin_tag


def _is_tagged(expr, tag):
    return (
        get_expr_builtin_tag(expr) == "link"
        and expr.tail is not None
        and get_expr_builtin_tag(expr.tail) == "symbol"
        and expr.tail.value == tag
    )


def _collect_link(value):
    out = []
    while get_expr_builtin_tag(value) == "link" and value.head is not None:
        out.append(value.head)
        if value.tail is NIL or value.tail is None:
            break
        value = value.tail
    return out


class TestSplitOperator(unittest.TestCase):
    """split operator — bytes, str, link; bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare split: bytes ---

    def test_split_bytes(self):
        """(0xdeadbeef 2 split) -> Link(left, right)."""
        expr, _ = parse(tokenize("(0xdeadbeef 2 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\xde\xad")
        self.assertEqual(get_expr_builtin_tag(result.tail), "bytes")
        self.assertEqual(result.tail.value, b"\xbe\xef")

    def test_split_left_kept_by_head(self):
        expr, _ = parse(tokenize("(0xabcdef 1 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\xab")

    def test_split_at_zero(self):
        expr, _ = parse(tokenize("(0xabcdef 0 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"")
        self.assertEqual(get_expr_builtin_tag(result.tail), "bytes")
        self.assertEqual(result.tail.value, b"\xab\xcd\xef")

    def test_split_at_full_length(self):
        expr, _ = parse(tokenize("(0xabcdef 3 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\xab\xcd\xef")
        self.assertEqual(get_expr_builtin_tag(result.tail), "bytes")
        self.assertEqual(result.tail.value, b"")

    # --- bare split: str ---

    def test_split_str(self):
        """("abcdef" 2 split) -> link("ab", "cdef")."""
        expr, _ = parse(tokenize('("abcdef" 2 split)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "ab")
        self.assertEqual(get_expr_builtin_tag(result.tail), "str")
        self.assertEqual(result.tail.value, "cdef")

    def test_split_str_codepoint_boundary(self):
        '''("café" 3 split) -> link("caf", "é") — splits at codepoint 3, not utf8 byte 3.'''
        expr, _ = parse(tokenize('("café" 3 split)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.head.value, "caf")
        self.assertEqual(result.tail.value, "é")

    # --- bare split: link ---

    def test_split_link(self):
        """('(a b c d) 2 split) -> link((a b), (c d))."""
        expr, _ = parse(tokenize("('(a b c d) 2 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        left, right = result.head, result.tail
        self.assertEqual([e.value for e in _collect_link(left)], ["a", "b"])
        self.assertEqual([e.value for e in _collect_link(right)], ["c", "d"])

    def test_split_link_at_zero(self):
        expr, _ = parse(tokenize("('(a b c) 0 split)"))
        result = self.machine.run(expr=expr)
        left, right = result.head, result.tail
        self.assertEqual(left, NIL)
        self.assertEqual([e.value for e in _collect_link(right)], ["a", "b", "c"])

    def test_split_link_at_length(self):
        expr, _ = parse(tokenize("('(a b c) 3 split)"))
        result = self.machine.run(expr=expr)
        left, right = result.head, result.tail
        self.assertEqual([e.value for e in _collect_link(left)], ["a", "b", "c"])
        self.assertEqual(right, NIL)

    # --- error cases ---

    def test_split_non_bytes_non_str_non_link_returns_nil(self):
        """(42 0 split) -> NIL (int has no split)."""
        expr, _ = parse(tokenize("(42 0 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_split_non_int_index_returns_nil(self):
        """(0xdead "x" split) -> NIL (index must be int)."""
        expr, _ = parse(tokenize('(0xdead "x" split)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_split_out_of_bounds_returns_nil(self):
        """(0xdead 5 split) -> NIL (out of bounds)."""
        expr, _ = parse(tokenize("(0xdead 5 split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_split_underflow_returns_nil(self):
        """(split) -> NIL."""
        expr, _ = parse(tokenize("(split)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged split (?) ---

    def test_split_bytes_ok_tagged(self):
        """(0xdeadbeef 2 split?) -> (ok . Link(left, right))."""
        expr, _ = parse(tokenize("(0xdeadbeef 2 split?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.head.value, b"\xde\xad")
        self.assertEqual(result.head.tail.value, b"\xbe\xef")

    def test_split_str_ok_tagged(self):
        """("abcdef" 2 split?) -> (ok . link("ab", "cdef"))."""
        expr, _ = parse(tokenize('("abcdef" 2 split?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.head.value, "ab")
        self.assertEqual(result.head.tail.value, "cdef")

    def test_split_link_ok_tagged(self):
        """('(a b c d) 2 split?) -> (ok . link((a b), (c d)))."""
        expr, _ = parse(tokenize("('(a b c d) 2 split?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head.head)], ["a", "b"])
        self.assertEqual([e.value for e in _collect_link(result.head.tail)], ["c", "d"])

    def test_split_type_err_tagged(self):
        '''(42 0 split?) -> (err . "split of int at int").'''
        expr, _ = parse(tokenize("(42 0 split?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "split of int at int")

    def test_split_oob_err_tagged(self):
        """(0xdead 5 split?) -> (err . "split index 5 out of bounds for bytes of length 2")."""
        expr, _ = parse(tokenize("(0xdead 5 split?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(
            result.head.value, "split index 5 out of bounds for bytes of length 2"
        )

    def test_split_underflow_err_tagged(self):
        """(split?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(split?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
