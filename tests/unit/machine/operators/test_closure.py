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


class TestClosureOperator(unittest.TestCase):
    """closure & apply — tagged function values."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- closure creates tagged link pair ---

    def test_closure_creates_tagged_value(self):
        """('(a) '(a 1 +) closure) pushes tagged link pair with 'lex tag."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "lex"))

    # --- apply invokes lambda value ---

    def test_apply_invokes_closure(self):
        """(42 '(a) '(a 1 +) closure apply) -> 43."""
        expr, _ = parse(tokenize("(42 '(a) '(a 1 +) closure apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    def test_apply_multiple_args(self):
        """(3 5 '(a b) '(a b +) closure apply) -> 8."""
        expr, _ = parse(tokenize("(3 5 '(a b) '(a b +) closure apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 8)

    # --- closure value via def ---

    def test_closure_via_def(self):
        """'(a b) '(a b +) closure 'add def then 3 5 add apply -> 8."""
        expr, _ = parse(tokenize("('(a b) '(a b +) closure 'add def 3 5 add apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 8)

    # --- env capture ---

    def test_closure_captures_env(self):
        """Outer 10 'x def, push 5, create closure, then apply -> 15."""
        expr, _ = parse(tokenize("(10 'x def 5 '(a) '(a x +) closure apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 15)

    # --- tagged dyn value via manual link ---

    def test_apply_tagged_dyn(self):
        """(3 5 '(a b +) '(a b) link 'dyn link apply) -> 8."""
        expr, _ = parse(tokenize("(3 5 '(a b +) '(a b) link 'dyn link apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 8)

    def test_apply_tagged_dyn_live_env(self):
        """dyn-tagged value sees parent env at apply time."""
        expr, _ = parse(tokenize("(10 'x def 5 '(a x +) '(a) link 'dyn link apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 15)

    def test_apply_tagged_pure(self):
        """pure-tagged value has parent=None (isolated); x is unbound -> NIL."""
        expr, _ = parse(tokenize("(10 'x def 5 '(a x +) '(a) link 'pure link apply)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    # --- errors ---

    def test_apply_non_function(self):
        """(42 apply) -> OpError("apply of int")."""
        expr, _ = parse(tokenize("(42 apply)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_apply_underflow(self):
        """(apply) -> OpError("stack underflow")."""
        expr, _ = parse(tokenize("(apply)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_apply_arg_underflow(self):
        """('(a) '(a 1 +) closure apply) -> OpError("stack underflow")."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure apply)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_apply_extra_args(self):
        """(7 3 5 '(a b) '(a b +) closure apply) -> 8 (7 stays on stack)."""
        expr, _ = parse(tokenize("(7 3 5 '(a b) '(a b +) closure apply)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 8)

    def test_closure_non_link_params(self):
        """(42 '(a 1 +) closure) -> OpError("closure of int")."""
        expr, _ = parse(tokenize("(42 '(a 1 +) closure)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_closure_underflow(self):
        """(closure) -> OpError("stack underflow")."""
        expr, _ = parse(tokenize("(closure)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    def test_closure_missing_body(self):
        """('(a) closure) -> OpError("stack underflow")."""
        expr, _ = parse(tokenize("('(a) closure)"))
        result = self.machine.run(expr=expr)
        self.assertIs(result, NIL)

    # --- deterministic mode ---

    def test_closure_deterministic(self):
        """closure creates a tagged link pair in deterministic mode."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure)"))
        self.machine.mode = "deterministic"
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "lex"))

    def test_apply_deterministic(self):
        """apply invokes closure in deterministic mode."""
        expr, _ = parse(tokenize("(42 '(a) '(a 1 +) closure apply)"))
        self.machine.mode = "deterministic"
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 43)

    # --- tagged forms (with_result) ---

    def test_closure_tagged_ok(self):
        """('(a) '(a 1 +) closure?) -> (tagged-link-pair . ok)."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertTrue(_is_tagged(result.head, "lex"))

    def test_closure_tagged_err(self):
        """(closure?) -> ("stack underflow" . err)."""
        expr, _ = parse(tokenize("(closure?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")

    def test_apply_tagged_ok(self):
        """(42 '(a) '(a 1 +) closure apply?) -> (43 . ok)."""
        expr, _ = parse(tokenize("(42 '(a) '(a 1 +) closure apply?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 43)

    def test_apply_tagged_err(self):
        """(apply?) -> ("stack underflow" . err)."""
        expr, _ = parse(tokenize("(apply?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "stack underflow")

    # --- function value properties ---

    def test_closure_value_type(self):
        """type returns symbol("lex") for tagged closure."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "lex")

    def test_closure_value_hash_not_operator(self):
        """hash is not an operator; returns (None . None)."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure hash)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_closure_value_bytes_not_supported(self):
        """bytes operator does not handle link tag."""
        expr, _ = parse(tokenize("('(a) '(a 1 +) closure bytes)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- nested apply with manual dyn tag ---

    def test_nested_apply(self):
        """Manual dyn-tagged value inside closure -> 26."""
        expr, _ = parse(tokenize(
            "(5 '(a) '(20 '(b 1 +) '(b) link 'dyn link apply a +) closure apply)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 26)


if __name__ == "__main__":
    unittest.main(verbosity=2)
