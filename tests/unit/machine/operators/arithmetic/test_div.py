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


class TestDivOperator(unittest.TestCase):
    """div (/) operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare div ---

    def test_div_int(self):
        """(100 7 /) -> 14."""
        expr, _ = parse(tokenize("(100 7 /)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 14)

    def test_div_float(self):
        """(10.0 4.0 /) -> 2.5."""
        expr, _ = parse(tokenize("(10.0 4.0 /)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp64")
        self.assertEqual(result.value, 2.5)

    def test_div_zero_returns_nil(self):
        """(7 0 /) -> NIL (bare dispatch catches OpError)."""
        expr, _ = parse(tokenize("(7 0 /)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_div_cross_type_returns_nil(self):
        """(1 "hello" /) -> NIL (bare dispatch catches OpError)."""
        expr, _ = parse(tokenize('(1 "hello" /)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_div_cross_fp64_int_returns_nil(self):
        """(1 2.0 /) -> NIL (cross-type Float/Int no longer allowed)."""
        expr, _ = parse(tokenize("(1 2.0 /)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_div_underflow_returns_nil(self):
        """(/) -> NIL."""
        expr, _ = parse(tokenize("(/)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged div (?) ---

    def test_div_int_ok(self):
        """(100 7 /?) -> (ok . 14)."""
        expr, _ = parse(tokenize("(100 7 /?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 14)

    def test_div_fp64_ok(self):
        """(10.0 4.0 /?) -> (ok . 2.5)."""
        expr, _ = parse(tokenize("(10.0 4.0 /?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "fp64")
        self.assertEqual(result.head.value, 2.5)

    def test_div_zero_err(self):
        """(7 0 /?) -> (err . "division by zero")."""
        expr, _ = parse(tokenize("(7 0 /?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "division by zero")

    def test_div_cross_type_err(self):
        """(1 "hello" /?) -> (err . "division by int and str")."""
        expr, _ = parse(tokenize("(1 \"hello\" /?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "division by int and str")

    def test_div_cross_fp64_int_err(self):
        """(1 2.0 /?) -> (err . "division by int and float")."""
        expr, _ = parse(tokenize("(1 2.0 /?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "division by int and fp64")

    def test_div_underflow_err(self):
        """(/?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(/?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)