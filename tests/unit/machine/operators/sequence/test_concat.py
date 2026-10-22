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
    """Collect cons-list into a flat Python list of head Exprs."""
    out = []
    while get_expr_builtin_tag(value) == "link" and value.head is not None:
        out.append(value.head)
        if value.tail is NIL or value.tail is None:
            break
        value = value.tail
    return out


class TestConcatOperator(unittest.TestCase):
    """concat operator — bytes, str, link; bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare concat: bytes ---

    def test_concat_bytes(self):
        """(0xdead 0xbeef concat) -> 0xde ad be ef."""
        expr, _ = parse(tokenize("(0xdead 0xbeef concat)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xde\xad\xbe\xef")

    # --- bare concat: str ---

    def test_concat_str(self):
        """("abc" "def" concat) -> "abcdef"."""
        expr, _ = parse(tokenize('("abc" "def" concat)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "abcdef")

    # --- bare concat: link ---

    def test_concat_link(self):
        """('(1 2) '(3 4) concat) -> '(1 2 3 4)."""
        expr, _ = parse(tokenize("('(1 2) '(3 4) concat)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual([e.value for e in _collect_link(result)], [1, 2, 3, 4])

    def test_concat_link_with_nils(self):
        """('(1 2) () concat) -> '(1 2); second arg must be a proper list."""
        expr, _ = parse(tokenize("('(1 2) () concat)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual([e.value for e in _collect_link(result)], [1, 2])

    # --- error cases ---

    def test_concat_non_bytes_returns_nil(self):
        """(1 0xdead concat) -> NIL."""
        expr, _ = parse(tokenize("(1 0xdead concat)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_concat_cross_tag_returns_nil(self):
        """(0xdead "x" concat) -> NIL (bytes vs str)."""
        expr, _ = parse(tokenize('(0xdead "x" concat)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_concat_underflow_returns_nil(self):
        """(concat) -> NIL."""
        expr, _ = parse(tokenize("(concat)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged concat (?) ---

    def test_concat_bytes_tagged(self):
        """(0xdead 0xbeef concat?) -> (ok . 0xdeadbeef)."""
        expr, _ = parse(tokenize("(0xdead 0xbeef concat?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\xde\xad\xbe\xef")

    def test_concat_str_tagged(self):
        """("abc" "def" concat?) -> (ok . "abcdef")."""
        expr, _ = parse(tokenize('("abc" "def" concat?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "abcdef")

    def test_concat_link_tagged(self):
        """('(1 2) '(3 4) concat?) -> (ok . '(1 2 3 4))."""
        expr, _ = parse(tokenize("('(1 2) '(3 4) concat?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head)], [1, 2, 3, 4])

    def test_concat_err_tagged(self):
        """(1 0xdead concat?) -> (err . "concatenation of int and bytes")."""
        expr, _ = parse(tokenize("(1 0xdead concat?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "concatenation of int and bytes")

    def test_concat_cross_tag_err_tagged(self):
        """(0xdead "x" concat?) -> (err . "concatenation of bytes and str")."""
        expr, _ = parse(tokenize('(0xdead "x" concat?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "concatenation of bytes and str")

    def test_concat_underflow_err_tagged(self):
        """(concat?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(concat?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
