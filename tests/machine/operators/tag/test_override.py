import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.machine.meter import MeterExceededError
from astreum.expression import get_expr_tag, get_expr_builtin_tag


class TestOperatorOverride(unittest.TestCase):
    """Test that user def bindings of operator names are evaluated as specs in dynamic mode."""

    def setUp(self):
        self.machine = Machine(node=None)

    def test_try_overridden_with_value(self):
        """def shadows try in non-det mode: try resolves to 42, not the operator."""
        expr, _ = parse(tokenize("(42 'try def '(\"fail\") try)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_ok_overridden_with_value(self):
        """def shadows ok in non-det mode: ok resolves to 99, not the constructor."""
        expr, _ = parse(tokenize("(99 'ok def 5 ok)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 99)

    def test_operator_override_runs_bound_program(self):
        """Dynamic mode evaluates a bound operator name: (0x01 'ok def 5 ok) -> 0x01."""
        expr, _ = parse(tokenize("(0x01 'ok def 5 ok)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x01")

    def test_quote_migration_recovers_push_semantics(self):
        """A quoted value restores push-semantics: ('('custom_msg) 'err def 5 err) -> custom_msg."""
        expr, _ = parse(tokenize("('('custom_msg) 'err def 5 err)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "custom_msg")

    def test_self_reference_metered_machine(self):
        """Self-referential binding recurses loudly: metered machine dies with MeterExceededError."""
        machine = Machine(node=None, meter_limit=1000)
        expr, _ = parse(tokenize("('ok 'ok def ok)"))
        with self.assertRaises(MeterExceededError):
            machine.run(expr=expr)

    def test_mutual_operator_pair_metered_machine(self):
        """Mutual operator pair recurses loudly: metered machine dies with MeterExceededError."""
        machine = Machine(node=None, meter_limit=1000)
        expr, _ = parse(tokenize("('err 'ok def 'ok 'err def ok)"))
        with self.assertRaises(MeterExceededError):
            machine.run(expr=expr)

    def test_deterministic_operator_pinned(self):
        """Deterministic mode pins the operator: the def is bound but never consulted."""
        machine = Machine(node=None, mode="deterministic")
        expr, _ = parse(tokenize("(99 'ok def 5 ok)"))
        result = machine.run(expr=expr)
        self.assertEqual(get_expr_tag(result), "ok")
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 5)

    def test_noop_binding_leaves_stack_as_is(self):
        """A binding of nil evaluates as a no-op: ('(nil) 'err def 5 err) -> 5."""
        expr, _ = parse(tokenize("('(nil) 'err def 5 err)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
