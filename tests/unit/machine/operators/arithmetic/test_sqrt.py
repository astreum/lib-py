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


class TestSqrtOperator(unittest.TestCase):
    """sqrt operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare sqrt ---

    def test_sqrt_float(self):
        """(9.0 sqrt) -> 3.0."""
        expr, _ = parse(tokenize("(9.0 sqrt)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp64")
        self.assertEqual(result.value, 3.0)

    def test_sqrt_negative_returns_nil(self):
        """(-1.0 sqrt) -> NIL (bare dispatch catches OpError)."""
        expr, _ = parse(tokenize("(-1.0 sqrt)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_sqrt_int_returns_nil(self):
        """(42 sqrt) -> NIL (int not allowed)."""
        expr, _ = parse(tokenize("(42 sqrt)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_sqrt_string_returns_nil(self):
        """("hello" sqrt) -> NIL (string not allowed)."""
        expr, _ = parse(tokenize('("hello" sqrt)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_sqrt_underflow_returns_nil(self):
        """(sqrt) -> NIL."""
        expr, _ = parse(tokenize("(sqrt)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged sqrt (?) ---

    def test_sqrt_fp64_ok(self):
        """(9.0 sqrt?) -> (ok . 3.0)."""
        expr, _ = parse(tokenize("(9.0 sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "fp64")
        self.assertEqual(result.head.value, 3.0)

    def test_sqrt_negative_err(self):
        """(-1.0 sqrt?) -> (err . "square root of negative number")."""
        expr, _ = parse(tokenize("(-1.0 sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "square root of negative number")

    def test_sqrt_int_err(self):
        """(42 sqrt?) -> (err . "square root of int")."""
        expr, _ = parse(tokenize("(42 sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "square root of int")

    def test_sqrt_string_err(self):
        """("hello" sqrt?) -> (err . "square root of str")."""
        expr, _ = parse(tokenize("(\"hello\" sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "square root of str")

    def test_sqrt_bytes_err(self):
        """(0xdead sqrt?) -> (err . "square root of bytes")."""
        expr, _ = parse(tokenize("(0xdead sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "square root of bytes")

    def test_sqrt_underflow_err(self):
        """(sqrt?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(sqrt?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)