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


class TestBitwiseXorOperator(unittest.TestCase):
    """bitwise xor operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare xor ---

    def test_xor(self):
        """(0x0f 0x33 ^) -> 0x3c."""
        expr, _ = parse(tokenize("(0x0f 0x33 ^)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")

    def test_xor_non_bytes_returns_nil(self):
        """(1 0xdead ^) -> NIL."""
        expr, _ = parse(tokenize("(1 0xdead ^)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_xor_underflow_returns_nil(self):
        """(^) -> NIL."""
        expr, _ = parse(tokenize("(^)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged xor (?) ---

    def test_xor_ok(self):
        """(0x0f 0x33 '^ try) -> (ok . 0x3c)."""
        expr, _ = parse(tokenize("(0x0f 0x33 ^?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")

    def test_xor_err(self):
        """(1 0xdead ^?) -> (err . "bitwise xor of int and bytes")."""
        expr, _ = parse(tokenize("(1 0xdead ^?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "bitwise xor of int and bytes")

    def test_xor_underflow_err(self):
        """(^?) -> (err . "stack underflow")."""
        expr, _ = parse(tokenize("(^?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)