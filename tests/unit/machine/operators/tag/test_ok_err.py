import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression import NIL
from astreum.expression.helpers import get_expr_builtin_tag
def _is_tagged(expr, tag):
    return (
        get_expr_builtin_tag(expr) == "link"
        and expr.tail is not None
        and get_expr_builtin_tag(expr.tail) == "symbol"
        and expr.tail.value == tag
    )


class TestOkOperator(unittest.TestCase):
    """ok operator: val -- (val . ok)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_ok_wraps(self):
        """(42 ok) -> (42 . ok)."""
        expr, _ = parse(tokenize("(42 ok)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 42)

    def test_ok_wraps_str(self):
        """("hello" ok) -> ("hello" . ok)."""
        expr, _ = parse(tokenize('("hello" ok)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "hello")

    def test_ok_underflow(self):
        """bare (ok) -> OpError caught, pushes NIL."""
        expr, _ = parse(tokenize("(ok)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)


class TestErrOperator(unittest.TestCase):
    """err operator: msg -- (msg . err)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_err_wraps(self):
        """("bad" err) -> ("bad" . err)."""
        expr, _ = parse(tokenize('("bad" err)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "bad")

    def test_err_underflow(self):
        """bare (err) -> OpError caught, pushes NIL."""
        expr, _ = parse(tokenize("(err)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)