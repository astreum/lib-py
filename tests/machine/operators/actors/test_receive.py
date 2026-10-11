import sys
import unittest
from pathlib import Path
from queue import Queue

ROOT = Path(__file__).resolve().parents[4]
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


class TestReceiveOperator(unittest.TestCase):
    def setUp(self):
        self.machine = Machine(node=None)

    def test_bare_non_symbol_target(self):
        expr, _ = parse(tokenize("(42 receive)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_bare_no_mailbox_nil(self):
        expr, _ = parse(tokenize("('nonexistent receive)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_success_bare(self):
        self.machine.mailboxes["target"] = Queue()
        self.machine.mailboxes["target"].put(int_(42))
        expr, _ = parse(tokenize("('target receive)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "int")
        self.assertEqual(result.value, 42)

    def test_tagged_non_symbol_target(self):
        expr, _ = parse(tokenize("(42 receive?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(get_expr_builtin_tag(result.head), "str")
        self.assertEqual(result.head.value, "receive target must be a symbol")

    def test_tagged_no_mailbox_ok_nil(self):
        expr, _ = parse(tokenize("('nonexistent receive?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "link")
        self.assertIsNone(result.head.head)
        self.assertIsNone(result.head.tail)

    def test_success_tagged(self):
        self.machine.mailboxes["target"] = Queue()
        self.machine.mailboxes["target"].put(int_(99))
        expr, _ = parse(tokenize("('target receive?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(get_expr_builtin_tag(result.head), "int")
        self.assertEqual(result.head.value, 99)


if __name__ == "__main__":
    unittest.main(verbosity=2)