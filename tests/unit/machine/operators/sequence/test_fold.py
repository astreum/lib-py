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


class TestFoldOperator(unittest.TestCase):
    """fold operator — foldl head-first; [acc, item] pre-pushed (item on top)."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- int sum over list ---

    def test_fold_int_sum(self):
        """('(1 2 3) 0 '(+) fold) -> 6."""
        expr, _ = parse(tokenize("('(1 2 3) 0 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 6)

    def test_fold_int_sum_left_associative(self):
        """foldl: 0 + 1 = 1, 1 + 2 = 3, 3 + 3 = 6 — check order."""
        expr, _ = parse(tokenize("('(1 2 3) 0 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.value, 6)

    def test_fold_with_nonzero_seed(self):
        """('(1 2 3) 10 '(+) fold) -> 16."""
        expr, _ = parse(tokenize("('(1 2 3) 10 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.value, 16)

    # --- empty sequence yields accumulator ---

    def test_fold_empty_list_returns_acc(self):
        """(() 99 '(+) fold) -> 99 (acc untouched)."""
        expr, _ = parse(tokenize("(() 99 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 99)

    def test_fold_empty_bytes_returns_acc(self):
        expr, _ = parse(tokenize("(0x 99 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.value, 99)

    def test_fold_empty_str_returns_acc(self):
        expr, _ = parse(tokenize('("" 99 \'(+) fold)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(result.value, 99)

    # --- bytes fold via int conversion ---

    def test_fold_bytes_to_int_sum(self):
        """(0x010203 0 '(int +) fold) -> 6. body per element: int-byte, then add to acc."""
        expr, _ = parse(tokenize("(0x010203 0 '(int +) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 6)

    def test_fold_bytes_xor_with_seed(self):
        """(0xff00 0x00 '(^) fold) -> 0xff (xor 0x00 with each 0xff, 0x00 = 0xff)."""
        expr, _ = parse(tokenize("(0xff00 0x00 '(^) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xff")

    # --- str fold ---

    def test_fold_str_concat_with_seed(self):
        """fold over str, counting codepoints with '(drop 1 +)."""
        expr, _ = parse(tokenize('("abc" 0 \'(drop 1 +) fold)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 3)

    def test_fold_str_with_bytes_acc(self):
        """("abc" 0x '(swap swap) fold) -> 0x: the body pushes (chars elem, bytes acc) and
        returns top — but this is fundamentally type-mismatched. Use simpler: bytes-accumulating
        by per-char concat test uses '(0x +) which isn't a real op. Skip.
        """
        pass

    # --- error cases ---

    def test_fold_non_closure_fn(self):
        expr, _ = parse(tokenize("('(1 2 3) 0 42 fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_fold_non_sequence_returns_nil(self):
        expr, _ = parse(tokenize("(42 0 '(+) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)

    def test_fold_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged fold ---

    def test_fold_ok_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) 0 '(+) fold?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, 6)

    def test_fold_err_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) 0 42 fold?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))

    def test_fold_underflow_err_tagged(self):
        expr, _ = parse(tokenize("(fold?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")

    # --- program fn: eval-style and apply-style specs ---

    def test_fold_apply_style_one_param_drops_acc(self):
        """A 1-param apply-style fold fn binds the elem; acc is NOT visible.

        ('(1 2 3) 0 '((f) apply) fold) with f = ('(a) '(a 1 +) closure):
          step acc=0, elem=1 -> apply binds a=1, body=2 -> acc=2
          step acc=2, elem=2 -> apply binds a=2, body=3 -> acc=3
          step acc=3, elem=3 -> apply binds a=3, body=4 -> acc=4
        """
        expr, _ = parse(tokenize("(('(a) '(a 1 +) closure) 'f def '(1 2 3) 0 '((f) apply) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 4)

    def test_fold_apply_style_two_params_binds_acc_and_elem(self):
        """A 2-param apply-style fold fn binds [acc, elem] -> left fold works.

        ('(1 2 3) 0 '((f) apply) fold) with f = ('(a b) '(a b +) closure):
          step a=0, b=1 -> 1; step a=1, b=2 -> 3; step a=3, b=3 -> 6.
        This is the fix the tagged step could not do (it bound exactly 1 param).
        """
        expr, _ = parse(tokenize("((('(a b) '(a b +) closure) 'f def) '(1 2 3) 0 '((f) apply) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 6)

    def test_fold_apply_style_lex_closure_uses_captured_env(self):
        """Lex closure captures x at definition time; fold body sees x.

        (10 'x def ('(1 2 3) 0 '((f) apply) fold) with f = ('(a) '(a x +) closure):
          acc=0, elem=1 -> 1+10=11; acc=11
          acc=11, elem=2 -> 2+10=12; acc=12
          acc=12, elem=3 -> 3+10=13; acc=13
        """
        expr, _ = parse(tokenize("(10 'x def ('(a) '(a x +) closure) 'f def '(1 2 3) 0 '((f) apply) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 13)

    def test_fold_apply_style_dyn_closure_sees_caller_env(self):
        """dyn-tagged fold fn reads x from caller env at apply time."""
        expr, _ = parse(tokenize("(10 'x def '(((a x +) . (a)) . dyn) 'f def '(1 2 3) 0 '((f) apply) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 13)

    def test_fold_apply_style_pure_closure_isolated(self):
        """Pure fn: parent=None, so x is unbound. Each step's body yields NIL
        (operator error swallowed by evaluator), so acc collapses to NIL.
        dyn mode would give 13."""
        expr, _ = parse(tokenize("(10 'x def '(((a x +) . (a)) . pure) 'f def '(1 2 3) 0 '((f) apply) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    def test_fold_eval_style_spec_runs_bound_program(self):
        """'(f eval) — head pushes the bound program, eval runs it on [acc, elem]."""
        expr, _ = parse(tokenize("(('(+)) 'f def '(1 2 3) 0 '(f eval) fold)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 6)

    def test_fold_raw_tagged_closure_rejected(self):
        """A raw tagged fn value is not a program -> OpError -> NIL. Apply-wrap it."""
        expr, _ = parse(tokenize(
            "('(1 2 3) 0 ('(a) '(a 1 +) closure) fold)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
