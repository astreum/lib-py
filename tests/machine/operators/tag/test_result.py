import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.expression import link, symbol, int_, str_, NIL
from astreum.expression.helpers import get_expr_builtin_tag


def _is_tagged(expr, tag):
    return (
        get_expr_builtin_tag(expr) == "link"
        and expr.tail is not None
        and get_expr_builtin_tag(expr.tail) == "symbol"
        and expr.tail.value == tag
    )


def _is_err(expr):
    return _is_tagged(expr, "err")


class TestResultTerminal(unittest.TestCase):
    """result operator: terminal unwrap (no continuation)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_unwrap_ok_int(self):
        """(42 ok result) -> 42."""
        expr, _ = parse(tokenize("(42 ok result)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_unwrap_ok_str(self):
        '''("hello" ok result) -> "hello".'''
        expr, _ = parse(tokenize('("hello" ok result)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "hello")

    def test_unwrap_ok_nil(self):
        """(() ok result) -> NIL."""
        expr, _ = parse(tokenize("(() ok result)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_underflow_empty(self):
        """bare (result) -> (msg . err)."""
        expr, _ = parse(tokenize("(result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_err(result))


class TestResultContinuation(unittest.TestCase):
    """result operator: bind with continuation."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_bind_with_continuation(self):
        """(2 ok '(3 +?) result) -> (5 . ok)."""
        expr, _ = parse(tokenize("(2 ok '(3 +?) result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 5)

    def test_chained_result(self):
        """(2 ok '(3 +?) result '(4 *?) result) -> (20 . ok)."""
        expr, _ = parse(tokenize("(2 ok '(3 +?) result '(4 *?) result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 20)


class TestResultErrors(unittest.TestCase):
    """result operator: error paths with err-tagged values."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_non_tagged_value(self):
        """(42 result) -> raw value, no tagged below -> error."""
        expr, _ = parse(tokenize("(42 result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_err(result))

    def test_empty_stack(self):
        """bare result with nothing on stack -> error."""
        expr = parse(tokenize("result"))[0]
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_err(result))

    def test_div_zero_forwards_err(self):
        """(1 0 /? result) -> (msg . err), terminal unwrap forwards."""
        expr, _ = parse(tokenize("(1 0 /? result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_err(result))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertIn("division by zero", result.head.value)

    def test_div_zero_short_circuits_continuation(self):
        """(1 0 /? '(3 +?) result) -> (msg . err), skips continuation."""
        expr, _ = parse(tokenize("(1 0 /? '(3 +?) result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_err(result))
        self.assertIn("division by zero", result.head.value)

    def test_success_chain_div_then_add(self):
        """(10 2 /? '(3 +?) result) -> (8 . ok), div succeeds then add."""
        expr, _ = parse(tokenize("(10 2 /? '(3 +?) result)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
