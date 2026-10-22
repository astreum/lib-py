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


class TestSubOperator(unittest.TestCase):
    """sub (-) operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare sub ---

    def test_sub_int(self):
        """(10 3 -) -> 7."""
        expr, _ = parse(tokenize("(10 3 -)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 7)

    def test_sub_float(self):
        """(5.5 2.0 -) -> 3.5."""
        expr, _ = parse(tokenize("(5.5 2.0 -)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp64")
        self.assertEqual(result.value, 3.5)

    def test_sub_cross_type_returns_nil(self):
        """(1 2.0 -) -> NIL (cross-type not allowed)."""
        expr, _ = parse(tokenize("(1 2.0 -)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_sub_string_int_returns_nil(self):
        """("hello" 1 -) -> NIL (type mismatch)."""
        expr, _ = parse(tokenize('("hello" 1 -)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_sub_underflow_returns_nil(self):
        """(-) -> NIL."""
        expr, _ = parse(tokenize("(-)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged sub (?) ---

    def test_sub_int_ok(self):
        """(10 3 -?) -> (ok . 7)."""
        expr, _ = parse(tokenize("(10 3 -?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 7)

    def test_sub_fp64_ok(self):
        """(5.5 2.0 -?) -> (ok . 3.5)."""
        expr, _ = parse(tokenize("(5.5 2.0 -?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "fp64")
        self.assertEqual(result.head.value, 3.5)

    def test_sub_cross_type_err(self):
        """(1 2.0 -?) -> (err . "subtraction of int and float")."""
        expr, _ = parse(tokenize("(1 2.0 -?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "subtraction of int and fp64")

    def test_sub_string_int_err(self):
        """("hello" 1 -?) -> (err . "subtraction of str and int")."""
        expr, _ = parse(tokenize("(\"hello\" 1 -?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "subtraction of str and int")

    def test_sub_underflow_err(self):
        """(-?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(-?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)