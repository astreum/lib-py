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


class TestSpecialFormQuote(unittest.TestCase):
    """' special form — prevents evaluation."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_quote_int(self):
        expr, _ = parse(tokenize("(' 42)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_quote_symbol(self):
        expr, _ = parse(tokenize("(' hello)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "hello")

    def test_quote_list(self):
        expr, _ = parse(tokenize("(' (1 2 3))"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")

    def test_quote_no_arg(self):
        expr, _ = parse(tokenize("(')"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_special_form_still_works(self):
        expr, _ = parse(tokenize("(' (1 2 add))"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 1)


class TestQuoteOperator(unittest.TestCase):
    """quote stack operator — wraps top of stack in (' ...)."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_quote_wraps_unbound_variable_as_nil(self):
        expr, _ = parse(tokenize("(hello quote)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "symbol")
        self.assertEqual(result.head.value, "'")
        self.assertEqual(get_expr_builtin_tag(result.tail), "link")
        self.assertIsNone(result.tail.head)
        self.assertIsNone(result.tail.tail)

    def test_quote_wraps_quoted_symbol(self):
        expr, _ = parse(tokenize("((' hello) quote)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "symbol")
        self.assertEqual(result.head.value, "'")
        self.assertEqual(get_expr_builtin_tag(result.tail), "symbol")
        self.assertEqual(result.tail.value, "hello")

    def test_quote_then_eval(self):
        expr, _ = parse(tokenize("((' 42) eval)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)