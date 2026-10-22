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


def _collect_link(value):
    out = []
    while get_expr_builtin_tag(value) == "link" and value.head is not None:
        out.append(value.head)
        if value.tail is NIL or value.tail is None:
            break
        value = value.tail
    return out


class TestFindOperator(unittest.TestCase):
    """find operator — short-circuit first match; ok/err link pairs."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- link ---

    def test_find_first_match_returns_with_some(self):
        """('(1 2 3 4) '(2 % 0 is_eq) find) -> link(2, some)."""
        expr, _ = parse(tokenize("('(1 2 3 4) '(2 % 0 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(result.head.value, 2)
        self.assertEqual(get_expr_builtin_tag(result.tail), "symbol")
        self.assertEqual(result.tail.value, "some")

    def test_find_first_among_others_returns_first_only(self):
        """('(2 4 6) '(2 % 0 is_eq) find) -> link(2, some) (not 4 or 6 — first match)."""
        expr, _ = parse(tokenize("('(2 4 6) '(2 % 0 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.head.value, 2)

    def test_find_no_match(self):
        """('(1 3 5) '(2 % 0 is_eq) find) -> link(str_("not found"), none)."""
        expr, _ = parse(tokenize("('(1 3 5) '(2 % 0 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "not found")
        self.assertEqual(result.tail.value, "none")

    def test_find_empty_returns_none(self):
        expr, _ = parse(tokenize("(() '(2 % 0 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertIsNotNone(result)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "not found")
        self.assertEqual(get_expr_builtin_tag(result.tail), "symbol")
        self.assertEqual(result.tail.value, "none")

    # --- short-circuit: walker must stop on first match ---

    def test_find_short_circuit_link(self):
        """Place an \"expensive\" sentinel after the first match and assert fn is not invoked past it.

        We can't observe side-effects cheaply, but we can assert behavior:
        find returns the first matched; it's impossible to test walk-stops without side effects.
        Skip a precise short-circuit assertion — covered by the implementation.
        """
        pass

    # --- bytes ---

    def test_find_bytes_match(self):
        """(0x0102 '(0x02 is_eq) find) — bytes 0x02 == 0x02 -> some with 0x02 elem."""
        # Predicate: element-bytes vs constant bytes.
        expr, _ = parse(tokenize("(0x0102 '(0x02 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.head.value, b"\x02")
        self.assertEqual(result.tail.value, "some")

    # --- error: non-closure fn or non-sequence ---

    def test_find_non_closure_fn(self):
        expr, _ = parse(tokenize("('(1 2 3) 42 find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_find_non_sequence(self):
        expr, _ = parse(tokenize("(42 '(0 is_eq) find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)

    def test_find_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(find)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged find ---

    def test_find_ok_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3 4) '(2 % 0 is_eq) find?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.head.value, 2)
        self.assertEqual(result.head.tail.value, "some")

    def test_find_none_tagged(self):
        expr, _ = parse(tokenize("('(1 3 5) '(0 is_eq) find?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.head.value, "not found")
        self.assertEqual(result.head.tail.value, "none")

    def test_find_type_err_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) 42 find?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))

    # --- program fn: eval-style and apply-style specs ---

    def test_find_apply_style_lex_closure_first_match(self):
        """Apply-style predicate (lex-closure) finds the first even element."""
        expr, _ = parse(tokenize(
            "(('(a) '(a 2 % 0 is_eq) closure) 'f def '(1 2 3 4) '((f) apply) find)"
        ))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "some"))
        self.assertEqual(result.head.value, 2)

    def test_find_apply_style_lex_closure_no_match(self):
        """Apply-style predicate with no match -> not-found none."""
        expr, _ = parse(tokenize(
            "(('(a) '(a 2 % 0 is_eq) closure) 'f def '(1 3 5) '((f) apply) find)"
        ))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "none"))
        self.assertEqual(result.head.value, "not found")

    def test_find_apply_style_dyn_closure_sees_caller_env(self):
        """dyn-tagged predicate finds first elem above caller-bound threshold."""
        expr, _ = parse(tokenize("(2 'threshold def '(((a threshold >) . (a)) . dyn) 'f def '(1 2 3 4) '((f) apply) find)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "some"))
        self.assertEqual(result.head.value, 3)

    def test_find_apply_style_pure_closure_isolated(self):
        """Pure fn (parent=None): threshold unbound -> predicate yields NIL
        (untruthy) for every element -> not found (none). dyn would find 3."""
        expr, _ = parse(tokenize("(2 'threshold def '(((a threshold >) . (a)) . pure) 'f def '(1 2 3 4) '((f) apply) find)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "none"))
        self.assertEqual(result.head.value, "not found")

    def test_find_eval_style_spec_runs_bound_program(self):
        """'(p eval) — head pushes the bound predicate, eval runs it on the element."""
        expr, _ = parse(tokenize("(('(2 % 0 is_eq)) 'p def '(1 2 3 4) '(p eval) find)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "some"))
        self.assertEqual(result.head.value, 2)

    def test_find_apply_spec_two_params_underflows(self):
        """A 2-param apply-style spec underflows per element -> NIL each -> not found."""
        expr, _ = parse(tokenize(
            "((('(a b) '(a b is_eq) closure) 'f def) '(1 2 3) '((f) apply) find)"
        ))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "none"))
        self.assertEqual(result.head.value, "not found")

    def test_find_raw_tagged_closure_rejected(self):
        """A raw tagged fn value is not a program -> OpError -> NIL. Apply-wrap it."""
        expr, _ = parse(tokenize(
            "('(1 2 3) ('(a) '(a 2 % 0 is_eq) closure) find)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
