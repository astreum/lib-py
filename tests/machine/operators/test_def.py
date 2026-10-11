import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
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


class TestDefOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare (no ?) ---

    def test_bare_def_success(self):
        expr, _ = parse(tokenize("(42 'x def)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_bare_def_binds_value(self):
        expr, _ = parse(tokenize("(42 'x def x)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_bare_def_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(def)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_bare_def_not_symbol_returns_nil(self):
        expr, _ = parse(tokenize("(42 99 def)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_bare_def_already_exists_returns_nil(self):
        """def inside a single run session: duplicate raises error (bare)."""
        expr, _ = parse(tokenize("(42 'x def 99 'x def)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_bare_def_first_binding_preserved(self):
        """def inside a single run session: first write wins."""
        expr, _ = parse(tokenize("(42 'x def 99 'x def x)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_def_isolated_across_runs(self):
        """each run() creates a fresh env; defs from one run do not persist."""
        self.machine.run(parse(tokenize("(42 'x def)"))[0])
        expr, _ = parse(tokenize("(x)"))
        result = self.machine.run(expr=expr)
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged (?) ---

    def test_tagged_def_success_returns_ok_nil(self):
        expr, _ = parse(tokenize("(42 'x def?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertIs(result.head, NIL)

    def test_tagged_def_binds_value(self):
        expr, _ = parse(tokenize("(42 'x def? x)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_tagged_def_underflow_returns_err(self):
        expr, _ = parse(tokenize("(def?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")

    def test_tagged_def_not_symbol_returns_err(self):
        expr, _ = parse(tokenize("(42 99 def?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "def of int")

    def test_tagged_def_already_exists_returns_err(self):
        """def? inside a single run session: duplicate raises err."""
        expr, _ = parse(tokenize("(42 'x def 7 'x def?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "def already exists")

    def test_top_def_isolated_per_run(self):
        """each Machine.run() call uses a fresh Env; no global shared namespace."""
        machine = Machine(node=None)
        machine.run(parse(tokenize("(42 'top_val def)"))[0])
        expr, _ = parse(tokenize("('top_val)"))
        result = machine.run(expr=expr)
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)