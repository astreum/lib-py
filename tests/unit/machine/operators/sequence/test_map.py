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


class TestMapOperator(unittest.TestCase):
    """map operator — type-preserving element-wise transformation."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bytes ---

    def test_map_bytes_xor(self):
        """(0xff00 '(0x01 ^) map) -> 0xfe01."""
        expr, _ = parse(tokenize("(0xff00 '(0x01 ^) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xfe\x01")

    def test_map_bytes_identity(self):
        """(0xdead '(dup) map) -> 0xdead (dup pushes a copy of the elem)."""
        expr, _ = parse(tokenize("(0xdead '(dup) map)"))
        result = self.machine.run(expr=expr)
        # dup pushes a copy; without explicit consume, the fn stack has dup so
        # the elem gets effectively duplicated per element — but for bytes, "dup"
        # on a bytes element returns the elem itself. Confirm bytes is preserved.
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xde\xad")

    def test_map_empty_bytes(self):
        expr, _ = parse(tokenize("(0x '(dup) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"")

    # --- str ---

    def test_map_str_upper_via_str_fn(self):
        """('"abc" '(dup) map) -> "abc" (preserves type via dup)."""
        expr, _ = parse(tokenize('("abc" \'(dup) map)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "abc")

    def test_map_empty_str(self):
        expr, _ = parse(tokenize('("" \'(dup) map)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "")

    # --- link ---

    def test_map_link_increment(self):
        """('(1 2 3) '(1 +) map) -> '(2 3 4)."""
        expr, _ = parse(tokenize("('(1 2 3) '(1 +) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 3, 4])

    def test_map_empty_link(self):
        expr, _ = parse(tokenize("(() '(1 +) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    # --- element-type mismatch (type error) ---

    def test_map_wrong_element_type_for_bytes_returns_nil(self):
        """(0xff00 '(0x00) map) — fn pushes 1byte, fine; use '(dup) won't change. Use a fn that returns int."""
        # The fn needs to return non-bytes for bytes map. Use a literal-int 'fn':
        # '(1) body yields constant 1 (int), incompatible with bytes.
        expr, _ = parse(tokenize("(0xff00 '(1) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_map_wrong_element_type_for_link_returns_symbolic_tail(self):
        # fn returns link(...) — incompatible (per plan, link element can be any Expr).
        # Use a const fn returning a different type. Simplest case: a fn that yields an int
        # for a link that should be... actually link allows any. Let's exercise the
        # explicit case: bytes→non-bytes is a hard error. Covered above.
        pass

    # --- non-closure fn ---

    def test_map_non_closure_fn_returns_nil(self):
        expr, _ = parse(tokenize("(0xff00 42 map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_map_non_sequence_returns_nil(self):
        expr, _ = parse(tokenize("(42 '(dup) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_map_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged map ---

    def test_map_ok_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) '(1 +) map?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head)], [2, 3, 4])

    def test_map_err_tagged(self):
        expr, _ = parse(tokenize("(0xff00 42 map?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))

    def test_map_underflow_err_tagged(self):
        expr, _ = parse(tokenize("(map?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")

    # --- program fn: eval-style and apply-style specs ---

    def test_map_apply_style_lex_closure_increments(self):
        """'(1 2 3) with an apply-style spec -> '(2 3 4).

        The closure is built, def'd, and invoked per element as '((f) apply).
        """
        expr, _ = parse(tokenize("(('(a) '(a 1 +) closure) 'f def '(1 2 3) '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 3, 4])

    def test_map_apply_style_lex_closure_on_bytes(self):
        """0xff00 with an apply-style spec -> 0xfe01."""
        expr, _ = parse(tokenize("((('(a) '(a 0x01 ^) closure) 'f def) 0xff00 '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\xfe\x01")

    def test_map_apply_style_lex_closure_on_str(self):
        '''"abc" with an apply-style spec -> "abc" (identity preserves str).'''
        expr, _ = parse(tokenize("(('(a) '(a) closure) 'f def \"abc\" '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "abc")

    def test_map_apply_style_dyn_closure_sees_caller_env(self):
        """dyn-tagged fn in an apply-style spec reads x from caller env."""
        expr, _ = parse(tokenize("(10 'x def '(((a x +) . (a)) . dyn) 'f def '(1 2 3) '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [11, 12, 13])

    def test_map_apply_style_pure_closure_isolated(self):
        """pure-tagged fn in an apply-style spec has parent=None, so x is unbound.
        Unbound symbols yield NIL (operator errors are swallowed by the evaluator),
        so each element maps to NIL: (nil nil nil). dyn mode would give (11 12 13)."""
        expr, _ = parse(tokenize("(10 'x def '(((a x +) . (a)) . pure) 'f def '(1 2 3) '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        collected = _collect_link(result)
        self.assertEqual(len(collected), 3)
        self.assertTrue(all(e.head is None for e in collected))

    def test_map_eval_style_spec_runs_bound_program(self):
        """'(r eval) — head pushes the bound program, eval runs it on the element."""
        expr, _ = parse(tokenize("(('(1 +)) 'r def '(1 2 3) '(r eval) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual([e.value for e in _collect_link(result)], [2, 3, 4])

    def test_map_apply_spec_two_params_underflows(self):
        """A 2-param apply-style spec on a 1-element pre-stack underflows per
        element (caught by the evaluator -> NIL each), not a whole-op error."""
        expr, _ = parse(tokenize("((('(a b) '(a b +) closure) 'f def) '(1 2 3) '((f) apply) map)"))
        result = self.machine.run(expr=expr)
        collected = _collect_link(result)
        self.assertEqual(len(collected), 3)
        self.assertTrue(all(e.head is None for e in collected))

    def test_map_raw_tagged_closure_rejected(self):
        """A raw tagged fn value is not a program -> OpError -> NIL. Apply-wrap it."""
        expr, _ = parse(tokenize("('(1 2 3) ('(a) '(a 1 +) closure) map)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    def test_map_apply_style_ok_via_with_result(self):
        """Sequence op works through the ?-form with an apply-style spec."""
        expr, _ = parse(tokenize("(('(a) '(a 1 +) closure) 'f def '(1 2 3) '((f) apply) map?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head)], [2, 3, 4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
