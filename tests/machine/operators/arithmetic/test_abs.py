import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
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


class TestAbsOperator(unittest.TestCase):
    """abs operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare abs ---

    def test_abs_int(self):
        """(-7 abs) -> 7."""
        expr, _ = parse(tokenize("(-7 abs)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 7)

    def test_abs_float(self):
        """(-3.5 abs) -> 3.5."""
        expr, _ = parse(tokenize("(-3.5 abs)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp64")
        self.assertEqual(result.value, 3.5)

    def test_abs_string_returns_nil(self):
        """("hello" abs) -> NIL (bare dispatch catches OpError)."""
        expr, _ = parse(tokenize('("hello" abs)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_abs_bytes_returns_nil(self):
        """(0xdead abs) -> NIL (bare dispatch catches OpError)."""
        expr, _ = parse(tokenize("(0xdead abs)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_abs_underflow_returns_nil(self):
        """(abs) -> NIL."""
        expr, _ = parse(tokenize("(abs)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged abs (?) ---

    def test_abs_int_ok(self):
        """(-7 abs?) -> (ok . 7)."""
        expr, _ = parse(tokenize("(-7 abs?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 7)

    def test_abs_fp64_ok(self):
        """(-3.5 abs?) -> (ok . 3.5)."""
        expr, _ = parse(tokenize("(-3.5 abs?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "fp64")
        self.assertEqual(result.head.value, 3.5)

    def test_abs_string_err(self):
        """("hello" abs?) -> (err . "absolute value of str")."""
        expr, _ = parse(tokenize("(\"hello\" abs?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "absolute value of str")

    def test_abs_bytes_err(self):
        """(0xdead abs?) -> (err . "absolute value of bytes")."""
        expr, _ = parse(tokenize("(0xdead abs?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "absolute value of bytes")

    def test_abs_underflow_err(self):
        """(abs?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(abs?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)