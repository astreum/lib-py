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


class TestSymbolOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_symbol_from_bytes(self):
        expr, _ = parse(tokenize('(0x68656c6c6f symbol)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "hello")

    def test_symbol_from_string(self):
        expr, _ = parse(tokenize('("hello" symbol)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "hello")

    def test_symbol_from_int(self):
        expr, _ = parse(tokenize("(42 symbol)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "42")

    def test_symbol_from_float(self):
        expr, _ = parse(tokenize("(3.14 symbol)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "3.14")

    def test_symbol_non_atom_returns_nil(self):
        expr, _ = parse(tokenize("(1 2 link symbol)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_symbol_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(symbol)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_symbol_from_bytes_ok(self):
        expr, _ = parse(tokenize("(0x68656c6c6f symbol?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "symbol")
        self.assertEqual(result.head.value, "hello")

    def test_symbol_utf8_err(self):
        expr, _ = parse(tokenize("(0x80ff symbol?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "symbol: bytes are not valid UTF-8")

    def test_symbol_non_atom_err(self):
        expr, _ = parse(tokenize("(1 2 link symbol?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "symbol of link")

    def test_symbol_underflow_err(self):
        expr, _ = parse(tokenize("(symbol?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)