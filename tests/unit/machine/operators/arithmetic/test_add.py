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


class TestAddOperator(unittest.TestCase):
    """add (+) operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare add ---

    def test_add_int(self):
        """(3 5 +) -> 8."""
        expr, _ = parse(tokenize("(3 5 +)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 8)

    def test_add_float(self):
        """(1.5 2.5 +) -> 4.0 (fp64 stays fp64)."""
        expr, _ = parse(tokenize("(1.5 2.5 +)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp64")
        self.assertEqual(result.value, 4.0)

    def test_add_fp16_same_precision(self):
        """(\"100\" fp16 \"200\" fp16 +) -> fp16 (not promoted to fp32)."""
        expr, _ = parse(tokenize('("100" fp16 "200" fp16 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp16")

    def test_add_fp16_overflow_nil(self):
        """(\"60000" fp16 \"60000" fp16 +) -> NIL (60000+60000 > 65504)."""
        expr, _ = parse(tokenize('("60000" fp16 "60000" fp16 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_add_fp16_max_overflow_nil(self):
        """(\"65504" fp16 \"1" fp16 +) -> NIL (65504+1 > max)."""
        expr, _ = parse(tokenize('("65504" fp16 "1" fp16 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_add_fp32_same_precision(self):
        """(\"1.5" fp32 \"2.5" fp32 +) -> fp32 (not promoted to fp64)."""
        expr, _ = parse(tokenize('("1.5" fp32 "2.5" fp32 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "fp32")

    def test_add_fp32_overflow_nil(self):
        """(\"2e38" fp32 \"2e38" fp32 +) -> NIL (sum > fp32 max)."""
        expr, _ = parse(tokenize('("2e38" fp32 "2e38" fp32 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_add_cross_type_returns_nil(self):
        """(1 2.0 +) -> NIL (cross-type not allowed)."""
        expr, _ = parse(tokenize("(1 2.0 +)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_add_string_int_returns_nil(self):
        '''("hello" 1 +) -> NIL (type mismatch).'''
        expr, _ = parse(tokenize('("hello" 1 +)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_add_underflow_returns_nil(self):
        """(+) -> NIL."""
        expr, _ = parse(tokenize("(+)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged add (?) ---

    def test_add_int_ok(self):
        """(3 5 +?) -> (8 . ok)."""
        expr, _ = parse(tokenize("(3 5 +?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 8)

    def test_add_fp64_ok(self):
        """(1.5 2.5 +?) -> (4.0 . ok)."""
        expr, _ = parse(tokenize("(1.5 2.5 +?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "fp64")
        self.assertEqual(result.head.value, 4.0)

    def test_add_cross_type_err(self):
        '''(1 2.0 +?) -> ("addition of int and fp64" . err).'''
        expr, _ = parse(tokenize("(1 2.0 +?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "addition of int and fp64")

    def test_add_string_int_err(self):
        '''("hello" 1 +?) -> ("addition of str and int" . err).'''
        expr, _ = parse(tokenize("(\"hello\" 1 +?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "addition of str and int")

    def test_add_underflow_err(self):
        """(+?) -> ("stack underflow" . err)."""
        expr, _ = parse(tokenize("(+?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")

    def test_add_fp16_overflow_err(self):
        '''("60000" fp16 "60000" fp16 +?) -> ("fp16 overflow" . err).'''
        expr, _ = parse(tokenize('("60000" fp16 "60000" fp16 +?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "fp16 overflow")

    def test_add_bf16_overflow_err(self):
        '''("3.3e38" bf16 "3.3e38" bf16 +?) -> ("bf16 overflow" . err).'''
        expr, _ = parse(tokenize('("3.3e38" bf16 "3.3e38" bf16 +?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "bf16 overflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)