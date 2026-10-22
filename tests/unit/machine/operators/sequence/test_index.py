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


class TestIndexOperator(unittest.TestCase):
    """index operator — bytes, str, link; bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare index: bytes ---

    def test_index_bytes(self):
        """(0xdeadbeef 0 index) -> 0xde."""
        expr, _ = parse(tokenize("(0xdeadbeef 0 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xde")

    def test_index_bytes_last(self):
        expr, _ = parse(tokenize("(0xdeadbeef 3 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xef")

    # --- bare index: str ---

    def test_index_str(self):
        '''("hello" 4 index) -> "o".'''
        expr, _ = parse(tokenize('("hello" 4 index)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "o")

    def test_index_str_codepoint_boundary(self):
        '''("café" 3 index) -> "é" — picks 4th codepoint, not 4th utf8 byte.'''
        expr, _ = parse(tokenize('("café" 3 index)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "é")

    # --- bare index: link ---

    def test_index_link(self):
        """('(a b c d) 2 index) -> c (symbol)."""
        expr, _ = parse(tokenize("('(a b c d) 2 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "c")

    def test_index_link_returns_complex_expr(self):
        """('((1 2) (3 4)) 1 index) -> (3 4). preserves any Expr as element."""
        expr, _ = parse(tokenize("('((1 2) (3 4)) 1 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(result.head.value, 3)
        self.assertEqual(result.tail.head.value, 4)

    # --- error cases ---

    def test_index_non_supported_type_returns_nil(self):
        """(42 0 index) -> NIL (int has no index)."""
        expr, _ = parse(tokenize("(42 0 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_index_non_int_returns_nil(self):
        '''("hello" "x" index) -> NIL (index must be int).'''
        expr, _ = parse(tokenize('("hello" "x" index)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_index_out_of_bounds_returns_nil(self):
        """(0xdead 5 index) -> NIL (out of bounds)."""
        expr, _ = parse(tokenize("(0xdead 5 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_index_negative_returns_nil(self):
        """(0xdead -1 index) -> NIL (negative index not allowed)."""
        expr, _ = parse(tokenize("(0xdead -1 index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_index_underflow_returns_nil(self):
        """(index) -> NIL."""
        expr, _ = parse(tokenize("(index)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged index (?) ---

    def test_index_bytes_ok_tagged(self):
        """(0xdeadbeef 0 index?) -> (ok . 0xde)."""
        expr, _ = parse(tokenize("(0xdeadbeef 0 index?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\xde")

    def test_index_str_ok_tagged(self):
        '''("hello" 4 index?) -> (ok . "o").'''
        expr, _ = parse(tokenize('("hello" 4 index?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, "o")

    def test_index_link_ok_tagged(self):
        """('(a b c d) 2 index?) -> (ok . c)."""
        expr, _ = parse(tokenize("('(a b c d) 2 index?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, "c")

    def test_index_type_err_tagged(self):
        '''("hello" "x" index?) -> (err . "index of str by str").'''
        expr, _ = parse(tokenize('("hello" "x" index?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "index of str by str")

    def test_index_oob_err_tagged(self):
        """(0xdead 5 index?) -> (err . "index 5 out of bounds for bytes of length 2")."""
        expr, _ = parse(tokenize("(0xdead 5 index?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "index 5 out of bounds for bytes of length 2")

    def test_index_underflow_err_tagged(self):
        """(index?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(index?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
