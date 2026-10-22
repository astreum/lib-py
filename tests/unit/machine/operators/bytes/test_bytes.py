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


class TestBytesConversionOperator(unittest.TestCase):
    """bytes conversion operator — bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare bytes ---

    def test_bytes_from_int(self):
        expr, _ = parse(tokenize("(42 bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")

    def test_bytes_from_float(self):
        expr, _ = parse(tokenize("(3.14 bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")

    def test_bytes_from_string(self):
        expr, _ = parse(tokenize('("hello" bytes)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")

    def test_bytes_from_bytes(self):
        expr, _ = parse(tokenize("(0xdead bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xde\xad")

    def test_bytes_link_returns_nil(self):
        """(foo bytes) -> NIL (unbound symbol pushes NIL, bytes fails on Link)."""
        expr, _ = parse(tokenize("(foo bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_bytes_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged bytes (?) ---

    def test_bytes_int_ok(self):
        expr, _ = parse(tokenize("(42 bytes?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")

    def test_bytes_link_err(self):
        """(foo bytes?) -> (err . "bytes of link") (unbound symbol pushes NIL/Link)."""
        expr, _ = parse(tokenize("(foo bytes?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "bytes of link")

    def test_bytes_underflow_err(self):
        expr, _ = parse(tokenize("(bytes?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)