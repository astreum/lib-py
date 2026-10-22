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


# --- << operator ---

class TestShiftOperator(unittest.TestCase):
    """<< shift operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_shift_bytes_left(self):
        expr, _ = parse(tokenize("(0x01 2 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x04")

    def test_shift_bytes_right_negative(self):
        expr, _ = parse(tokenize("(0x04 -2 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x01")

    def test_shift_bytes_zero(self):
        expr, _ = parse(tokenize("(0xab 0 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xab")

    def test_shift_int_left(self):
        expr, _ = parse(tokenize("(42 2 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 168)

    def test_shift_int_right_negative(self):
        expr, _ = parse(tokenize("(42 -2 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 10)

    def test_shift_int_zero(self):
        expr, _ = parse(tokenize("(42 0 <<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_shift_type_err_returns_nil(self):
        expr, _ = parse(tokenize('("hi" 2 <<)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_shift_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_shift_bytes_left_ok(self):
        expr, _ = parse(tokenize("(0x01 2 <<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\x04")

    def test_shift_bytes_right_negative_ok(self):
        expr, _ = parse(tokenize("(0x04 -2 <<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\x01")

    def test_shift_int_left_ok(self):
        expr, _ = parse(tokenize("(42 2 <<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 168)

    def test_shift_type_err(self):
        expr, _ = parse(tokenize("(\"hi\" 2 <<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "shift of str by int")

    def test_shift_amount_err(self):
        expr, _ = parse(tokenize("(0x01 \"hi\" <<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "shift of bytes by str")

    def test_shift_underflow_err(self):
        expr, _ = parse(tokenize("(<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


# --- <<< operator ---

class TestRotateOperator(unittest.TestCase):
    """<<< rotate operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_rotate_bytes_left(self):
        expr, _ = parse(tokenize("(0x01 2 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x04")

    def test_rotate_bytes_right_negative(self):
        expr, _ = parse(tokenize("(0x01 -2 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x40")

    def test_rotate_bytes_zero(self):
        expr, _ = parse(tokenize("(0xab 0 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xab")

    def test_rotate_bytes_full_wrap(self):
        expr, _ = parse(tokenize("(0x01 8 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x01")

    def test_rotate_int_left(self):
        expr, _ = parse(tokenize("(1 2 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 4)

    def test_rotate_int_right_negative(self):
        expr, _ = parse(tokenize("(1 -2 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 64)

    def test_rotate_int_zero(self):
        expr, _ = parse(tokenize("(42 0 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_rotate_int_signed_preserved(self):
        expr, _ = parse(tokenize("(-1 4 <<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, -1)

    def test_rotate_type_err_returns_nil(self):
        expr, _ = parse(tokenize('("hi" 2 <<<)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_rotate_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(<<<)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_rotate_bytes_left_ok(self):
        expr, _ = parse(tokenize("(0x01 2 <<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\x04")

    def test_rotate_bytes_right_negative_ok(self):
        expr, _ = parse(tokenize("(0x01 -2 <<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(result.head.value, b"\x40")

    def test_rotate_int_left_ok(self):
        expr, _ = parse(tokenize("(1 2 <<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 4)

    def test_rotate_type_err(self):
        expr, _ = parse(tokenize("(\"hi\" 2 <<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "rotate of str by int")

    def test_rotate_amount_err(self):
        expr, _ = parse(tokenize("(0x01 \"hi\" <<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "rotate of bytes by str")

    def test_rotate_underflow_err(self):
        expr, _ = parse(tokenize("(<<<?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)