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


class TestRecOperator(unittest.TestCase):
    """rec — recursive combinator, bare and tagged (?)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bare success ---

    def test_bare_factorial(self):
        """(5 '(dup 0 is_eq) '(drop 1) '(dup 1 -) '(swap *) rec) -> Int(120)."""
        expr, _ = parse(tokenize("(5 '(dup 0 is_eq) '(drop 1) '(dup 1 -) '(swap *) rec)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 120)

    def test_bare_sum(self):
        """(5 '(dup 0 is_eq) '(drop 0) '(dup 1 -) '(+) rec) -> Int(15)."""
        expr, _ = parse(tokenize("(5 '(dup 0 is_eq) '(drop 0) '(dup 1 -) '(+) rec)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 15)

    # --- bare underflow -> NIL ---

    def test_bare_underflow(self):
        """(rec) -> NIL."""
        expr, _ = parse(tokenize("(rec)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_bare_partial_underflow(self):
        """(1 2 rec) -> NIL."""
        expr, _ = parse(tokenize("(1 2 rec)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    # --- tagged success -> (ok ...) ---

    def test_tagged_success(self):
        """rec? -> (ok Int(120))."""
        expr, _ = parse(tokenize("(5 '(dup 0 is_eq) '(drop 1) '(dup 1 -) '(swap *) rec?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 120)

    # --- tagged underflow -> (err ...) ---

    def test_tagged_underflow(self):
        """(rec?) -> (err "stack underflow")."""
        expr, _ = parse(tokenize("(rec?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")

    # --- deep recursion — iterative fix prevents RecursionError ---

    def test_deep_sum(self):
        """Sum 1..2000 with rec — would hit Python recursion limit (~1000) before the fix."""
        expr, _ = parse(
            tokenize("(2000 '(dup 0 is_eq) '(drop 0) '(dup 1 -) '(+) rec)")
        )
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 2001000)



if __name__ == "__main__":
    unittest.main(verbosity=2)