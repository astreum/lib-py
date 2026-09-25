import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine import Expr, tokenize, parse
from astreum.machine.main import Machine
from astreum.machine.environment import Env, env_to_radix_tree
from astreum.expression import NIL, int_, symbol, get_expr_tag


class TestEnvHash(unittest.TestCase):
    """Content-hashed env references: radix-backed, dedup, tagged."""

    def setUp(self):
        self.machine = Machine(node=None)

    # --- deterministic hash ---

    def test_hash_deterministic_across_insertion_order(self):
        """Same bindings, different insertion order -> same chain hash."""
        a = Env()
        a.put("x", int_(10))
        a.put("y", int_(20))
        b = Env()
        b.put("y", int_(20))
        b.put("x", int_(10))
        self.assertEqual(
            env_to_radix_tree(a, None).hash(),
            env_to_radix_tree(b, None).hash(),
        )

    # --- distinct hash ---

    def test_distinct_bindings_give_distinct_hash(self):
        """Chains differing by one binding -> different chain hashes."""
        a = Env()
        a.put("x", int_(10))
        b = Env()
        b.put("x", int_(11))
        self.assertNotEqual(
            env_to_radix_tree(a, None).hash(),
            env_to_radix_tree(b, None).hash(),
        )

    # --- chain composition ---

    def test_chain_composition_differs_by_parent(self):
        """Same leaf level, different parents -> different chain hashes."""
        leaf1 = Env()
        leaf1.put("x", int_(1))
        leaf2 = Env()
        leaf2.put("x", int_(2))  # different parent bindings -> different parent hash
        p1 = env_to_radix_tree(leaf1, None).hash()
        p2 = env_to_radix_tree(leaf2, None).hash()
        child = Env()
        child.put("y", int_(2))
        h1 = env_to_radix_tree(child, None, parent_hash=p1).hash()
        h2 = env_to_radix_tree(child, None, parent_hash=p2).hash()
        self.assertNotEqual(h1, h2)

    # --- parent not in key space ---

    def test_parent_not_in_bindings_key_space(self):
        """Parent hash rides on level.head_hash, not as a binding."""
        parent = Env()
        parent.put("x", int_(10))
        parent_hash = env_to_radix_tree(parent, None).hash()
        child = Env(parent=parent)
        child.put("y", int_(20))
        child_expr = env_to_radix_tree(child, None, parent_hash=parent_hash)
        # level = child_expr.head; parent hash rides on level's head_hash
        self.assertEqual(child_expr.head.head_hash, parent_hash)

    # --- env tag ---

    def test_env_expr_tagged_env(self):
        """env_to_radix_tree returns a value tagged 'env'."""
        e = Env()
        e.put("x", int_(1))
        self.assertEqual(get_expr_tag(env_to_radix_tree(e, None)), "env")

    # --- dedup ---

    def test_snapshot_dedup(self):
        """Two snapshot_env calls on identical chains -> same hash, one entry."""
        a = Env()
        a.put("x", int_(10))
        b = Env()
        b.put("x", int_(10))
        h1 = self.machine.snapshot_env(a)
        lib_size = len(self.machine.library)
        h2 = self.machine.snapshot_env(b)
        self.assertEqual(h1, h2)
        self.assertEqual(len(self.machine.library), lib_size)

    # --- frozen at capture ---

    def test_closure_frozen_at_capture(self):
        """def after capture does not leak into the closure's scope."""
        expr, _ = parse(tokenize(
            "(10 'x def "
            "'(a) '(a x +) closure 'f def "
            "20 'y def "
            "5 f apply)"
        ))
        result = self.machine.run(expr=expr)
        self.assertEqual(result._tag, "int")
        self.assertEqual(result.value, 15)

    def test_post_capture_def_unbound_in_closure(self):
        """A symbol defined after capture resolves unbound inside the closure."""
        expr, _ = parse(tokenize(
            "(10 'x def "
            "'(a) '(a y +) closure 'f def "
            "20 'y def "
            "5 f apply)"
        ))
        result = self.machine.run(expr=expr)
        # y is unbound at capture -> closure body sees NIL for y -> add fails -> NIL
        self.assertIs(result, NIL)

    # --- apply guard ---

    def test_apply_rejects_non_env_capture(self):
        """apply on a lex closure whose capture is not tagged env -> OpError -> NIL."""
        from astreum.expression import bytes_, link
        from astreum.machine.operators.apply import handle_stack_apply
        # Build a lex closure whose capture is tagged 'notenv', not 'env'.
        params, _ = parse(tokenize("'(a)"))
        body, _ = parse(tokenize("'(a 1 +)"))
        bad_capture = link(bytes_(b"\x00" * 32), symbol("notenv"))
        body_with_capture = link(bad_capture, body)
        closure = link(link(body_with_capture, params), symbol("lex"))
        stack = [int_(5), closure]
        with self.assertRaises(Exception):
            handle_stack_apply(self.machine, stack, Env())


if __name__ == "__main__":
    unittest.main(verbosity=2)
