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


class TestMatchOperator(unittest.TestCase):

    def setUp(self):
        self.machine = Machine(node=None)

    # --- success: tag matches ---

    def test_match_ok_success(self):
        """((42 ok) 'ok '(1 +) '(drop -1) match) -> 43."""
        expr, _ = parse(tokenize("((42 ok) 'ok '(1 +) '(drop -1) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    def test_match_err_success(self):
        """((42 err) 'err '(1 +) '(drop -1) match) -> 43."""
        expr, _ = parse(tokenize("((42 err) 'err '(1 +) '(drop -1) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    # (some/none tests removed — those operators were never implemented)

    # --- failure: tag does not match, val pushed, fail-cl runs ---

    def test_match_tag_mismatch_fail_cl_gets_val(self):
        """((42 err) 'ok '(1 +) '(head) match) -> 42 (fail-cl gets val)."""
        expr, _ = parse(tokenize("((42 err) 'ok '(1 +) '(head) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_match_tag_mismatch_drop_val(self):
        """((42 err) 'ok '(1 +) '(drop 0) match) -> 0 (fail-cl drops val, pushes 0)."""
        expr, _ = parse(tokenize("((42 err) 'ok '(1 +) '(drop 0) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 0)

    def test_match_untagged_val_goes_to_fail_cl(self):
        """(42 'ok '(1 +) '(drop 42) match) -> 42 (untagged always fails, val pushed to fail-cl)."""
        expr, _ = parse(tokenize("(42 'ok '(1 +) '(drop 42) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    # --- cascade: fail-cl does inner match via dup ---

    def test_match_cascade_ok_then_err(self):
        """((42 err) 'ok '(1 +) '('err '(drop 0) '(drop -1) match) match) -> 0."""
        expr, _ = parse(tokenize("((42 err) 'ok '(1 +) '('err '(drop 0) '(drop -1) match) match)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 0)

    def test_match_cascade_all_fail(self):
        """val is some, match ok fails, match err fails, fallthrough drops."""
        src = """((42 some) 'ok '(1 +) '('err '(drop 0) '(drop -1) match) match)"""
        expr, _ = parse(tokenize(src))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, -1)

    # --- string tag names ---

    def test_match_string_tag(self):
        """((42 ok) "ok" '(1 +) '(drop -1) match) -> 43."""
        expr, _ = parse(tokenize('((42 ok) "ok" \'(1 +) \'(drop -1) match)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    # --- underflow ---

    def test_match_underflow(self):
        """bare (match) -> NIL."""
        expr, _ = parse(tokenize("(match)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_match_not_enough_args(self):
        """((42 ok) 'ok '(1 +) match) -> NIL (need 4 items, only 3)."""
        expr, _ = parse(tokenize("((42 ok) 'ok '(1 +) match)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)


class TestMatchDeterministic(unittest.TestCase):

    def test_match_deterministic_success(self):
        """deterministic: ((42 ok) 'ok '(1 +) '(drop -1) match) -> 43."""
        machine = Machine(node=None, mode="deterministic")
        expr, _ = parse(tokenize("((42 ok) 'ok '(1 +) '(drop -1) match)"))
        result = machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    def test_match_deterministic_fail(self):
        """deterministic: mismatch -> val on stack, fail-cl runs."""
        machine = Machine(node=None, mode="deterministic")
        expr, _ = parse(tokenize("((42 err) 'ok '(1 +) '(head) match)"))
        result = machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
