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


class TestIdOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_id_of_int(self):
        expr, _ = parse(tokenize("(42 id)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(len(result.value), 32)

    def test_id_of_link(self):
        expr, _ = parse(tokenize("(1 2 link id)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(len(result.value), 32)

    def test_id_of_symbol(self):
        expr, _ = parse(tokenize('(42 int id)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(len(result.value), 32)

    def test_id_deterministic(self):
        expr, _ = parse(tokenize("(42 id)"))
        r1 = self.machine.run(expr=expr)
        expr2, _ = parse(tokenize("(42 id)"))
        r2 = self.machine.run(expr=expr2)
        self.assertEqual(r1.value, r2.value)

    def test_id_roundtrip(self):
        expr, _ = parse(tokenize("(42 id id)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(len(result.value), 32)

    def test_id_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(id)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_id_ok(self):
        expr, _ = parse(tokenize("(42 id?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "bytes")
        self.assertEqual(len(result.head.value), 32)

    def test_id_underflow_err(self):
        expr, _ = parse(tokenize("(id?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)