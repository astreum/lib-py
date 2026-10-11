import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression.helpers import get_expr_builtin_tag


def _is_tagged(expr, tag):
    return (
        get_expr_builtin_tag(expr) == "link"
        and expr.tail is not None
        and get_expr_builtin_tag(expr.tail) == "symbol"
        and expr.tail.value == tag
    )


def _is_nil(expr):
    return get_expr_builtin_tag(expr) == "link" and expr.head is None and expr.tail is None


class TestDipOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_dip_basic(self):
        expr, _ = parse(tokenize("(3 4 (' (dup mul)) dip)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 4)

    def test_dip_ok_nil(self):
        expr, _ = parse(tokenize("(3 4 (' (dup mul)) dip?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 4)

    def test_dip_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(dip)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_nil(result))

    def test_dip_underflow_err(self):
        expr, _ = parse(tokenize("(dip?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)