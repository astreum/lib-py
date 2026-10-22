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


class TestFilterOperator(unittest.TestCase):
    """filter operator — keep elements whose fn (truthy per is_truthy) returns truthy."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- link ---

    def test_filter_keep_even_ints(self):
        """('(1 2 3 4) '(2 % 0 is_eq) filter) -> '(2 4)."""
        expr, _ = parse(tokenize("('(1 2 3 4) '(2 % 0 is_eq) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 4])

    def test_filter_none_match(self):
        """('(1 3 5) '(0 is_eq) filter) -> () — none are equal to 0."""
        expr, _ = parse(tokenize("('(1 3 5) '(0 is_eq) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    def test_filter_all_match(self):
        """('(1 2 3) '(1 is_eq) filter) — none equal 1, but pick first-match conceptually.
        Use a fn that is always true:
        """
        expr, _ = parse(tokenize("('(1 2 3) '(0 is_eq 0x01 ^) filter)"))
        # '(0 is_eq 0x01 ^) over an int x: x, 0, is_eq=false, 0x01 ^ -> bytes(b'\x01') which is truthy.
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [1, 2, 3])

    def test_filter_empty_list(self):
        expr, _ = parse(tokenize("(() '(0 is_eq) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    # --- bytes ---

    def test_filter_bytes_keep_nonzero(self):
        """(0x010001 '(dup) filter) — keep bytes whose dup'd value is truthy.

        For bytes, is_truthy: int.from_bytes != 0. So 0x01 >= 1 byte, encoded int
        is 1, truthy; 0x00 byte gives int 0, not truthy.
        """
        # Body '(dup)': push elem, dup it. Trues per `is_truthy`.
        expr, _ = parse(tokenize("(0x01000100 '(dup) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x01\x01")

    def test_filter_empty_bytes(self):
        expr, _ = parse(tokenize("(0x '(dup) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"")

    # --- str ---

    def test_filter_str_always_truthy(self):
        '''("abc" '(dup) filter) -> "abc" — for str, is_truthy always true.'''
        expr, _ = parse(tokenize('("abc" \'(dup) filter)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "abc")

    # --- error / bad input ---

    def test_filter_non_closure_fn(self):
        expr, _ = parse(tokenize("('(1 2 3) 42 filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_filter_non_sequence(self):
        expr, _ = parse(tokenize("(42 '(0 is_eq) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)

    def test_filter_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged filter ---

    def test_filter_ok_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3 4) '(2 % 0 is_eq) filter?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head)], [2, 4])

    def test_filter_err_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) 42 filter?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))

    def test_filter_underflow_err_tagged(self):
        expr, _ = parse(tokenize("(filter?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")

    # --- program fn: eval-style and apply-style specs ---

    def test_filter_apply_style_lex_closure_even_numbers(self):
        """'(1 2 3 4) with an apply-style spec -> '(2 4)."""
        expr, _ = parse(tokenize(
            "(('(a) '(a 2 % 0 is_eq) closure) 'f def '(1 2 3 4) '((f) apply) filter)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 4])

    def test_filter_apply_style_dyn_closure_sees_caller_env(self):
        """dyn-tagged fn in an apply-style spec compares elem to caller threshold."""
        expr, _ = parse(tokenize("(2 'threshold def '(((a threshold >=) . (a)) . dyn) 'f def '(1 2 3 4) '((f) apply) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 3, 4])

    def test_filter_apply_style_pure_closure_isolated(self):
        """Pure fn has no env; threshold unbound -> predicate yields NIL
        (untruthy) for every element -> nothing kept -> NIL. dyn keeps 2 3 4."""
        expr, _ = parse(tokenize("(2 'threshold def '(((a threshold >=) . (a)) . pure) 'f def '(1 2 3 4) '((f) apply) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    def test_filter_eval_style_spec_runs_bound_program(self):
        """'(p eval) — head pushes the bound predicate, eval runs it on the element."""
        expr, _ = parse(tokenize("(('(2 % 0 is_eq)) 'p def '(1 2 3 4) '(p eval) filter)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 4])

    def test_filter_apply_spec_two_params_underflows(self):
        """A 2-param apply-style spec underflows per element -> NIL each -> kept
        nothing (NIL is falsy)."""
        expr, _ = parse(tokenize(
            "((('(a b) '(a b is_eq) closure) 'f def) '(1 2 3) '((f) apply) filter)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    def test_filter_raw_tagged_closure_rejected(self):
        """A raw tagged fn value is not a program -> OpError -> NIL. Apply-wrap it."""
        expr, _ = parse(tokenize(
            "('(1 2 3) ('(a) '(a 2 % 0 is_eq) closure) filter)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
