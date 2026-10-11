import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.machine.main import Machine
from astreum.machine.parser import parse
from astreum.machine.tokenizer import tokenize
from astreum.expression import Expr, int_, fp64_, str_, symbol, bytes_, link, NIL, get_expr_tag, get_expr_builtin_tag

import unittest


class TestInitOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None, mode="deterministic")

    def test_init_new_tag(self):
        """(3 'point init) -> link(3, symbol("point"))."""
        expr, _ = parse(tokenize("(3 'point init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_tag(result), "point")
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 3)

    def test_init_idempotent(self):
        """(42 'int init) -> Int(42) (idempotent)."""
        expr, _ = parse(tokenize("(42 'int init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_init_link_identity(self):
        """((3 5 link) 'link init) -> same link (identity)."""
        expr, _ = parse(tokenize("((3 5 link) 'link init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual(result.head.value, 3)

    def test_init_tag_symbol(self):
        """('hello 'symbol init) -> Symbol("hello") (idempotent)."""
        expr, _ = parse(tokenize("('hello 'symbol init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "hello")

    def test_init_rejects_non_symbol_tag(self):
        """(42 3 init) -> NIL (error caught, tag must be symbol)."""
        expr, _ = parse(tokenize("(42 3 init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_init_underflow(self):
        """(init) -> NIL (error caught, stack underflow)."""
        expr, _ = parse(tokenize("(init)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)


class TestTypeOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None, mode="deterministic")

    def test_type_int(self):
        """(42 type) -> Symbol("int")."""
        expr, _ = parse(tokenize("(42 type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "int")

    def test_type_float(self):
        """(3.14 type) -> Symbol("fp64")."""
        expr, _ = parse(tokenize("(3.14 type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "fp64")

    def test_type_str(self):
        '''("hello" type) -> Symbol("str").'''
        expr, _ = parse(tokenize('("hello" type)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "str")

    def test_type_symbol(self):
        """('x type) -> Symbol("symbol")."""
        expr, _ = parse(tokenize("('x type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "symbol")

    def test_type_link(self):
        """((3 5 link) type) -> Symbol("link")."""
        expr, _ = parse(tokenize("((3 5 link) type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "link")

    def test_type_user_type(self):
        """((3 'point init) type) -> Symbol("point")."""
        expr, _ = parse(tokenize("((3 'point init) type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "symbol")
        self.assertEqual(result.value, "point")

    def test_type_underflow(self):
        """(type) -> NIL (error caught, stack underflow)."""
        expr, _ = parse(tokenize("(type)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)


if __name__ == "__main__":
    unittest.main()