import sys
import unittest
from pathlib import Path

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


def _collect_link(value):
    out = []
    while get_expr_builtin_tag(value) == "link" and value.head is not None:
        out.append(value.head)
        if value.tail is NIL or value.tail is None:
            break
        value = value.tail
    return out


class TestReverseOperator(unittest.TestCase):
    """reverse operator — type-preserving across bytes/str/link.

    Note: reverses codepoints / bytes / cons-cells, not grapheme clusters.
    Combining-accent strings (e.g. "é" composed) split on reverse.
    """

    def setUp(self):
        self.machine = Machine(node=None)

    # --- bytes ---

    def test_reverse_bytes(self):
        """(0x010203 reverse) -> 0x030201."""
        expr, _ = parse(tokenize("(0x010203 reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"\x03\x02\x01")

    def test_reverse_empty_bytes(self):
        expr, _ = parse(tokenize("(0x reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "bytes")
        self.assertEqual(result.value, b"")

    # --- str ---

    def test_reverse_str(self):
        '''("abc" reverse) -> "cba".'''
        expr, _ = parse(tokenize('("abc" reverse)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "cba")

    def test_reverse_str_codepoint_boundaries(self):
        '''("café" reverse) -> "éfac" — codepoint, not utf8-byte reversal.'''
        expr, _ = parse(tokenize('("café" reverse)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "éfac")

    def test_reverse_empty_str(self):
        expr, _ = parse(tokenize('("" reverse)'))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "str")
        self.assertEqual(result.value, "")

    # --- link ---

    def test_reverse_link(self):
        """('(1 2 3) reverse) -> '(3 2 1)."""
        expr, _ = parse(tokenize("('(1 2 3) reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertEqual([e.value for e in _collect_link(result)], [3, 2, 1])

    def test_reverse_empty_link(self):
        expr, _ = parse(tokenize("(() reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(result, NIL)

    # --- error: non-sequence tags ---

    def test_reverse_int_returns_nil(self):
        expr, _ = parse(tokenize("(42 reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    def test_reverse_underflow_returns_nil(self):
        expr, _ = parse(tokenize("(reverse)"))
        result = self.machine.run(expr=expr)
        self.assertEqual(get_expr_builtin_tag(result), "link")
        self.assertIsNone(result.head)
        self.assertIsNone(result.tail)

    # --- tagged reverse ---

    def test_reverse_bytes_ok_tagged(self):
        expr, _ = parse(tokenize("(0x010203 reverse?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, b"\x03\x02\x01")

    def test_reverse_str_ok_tagged(self):
        expr, _ = parse(tokenize('("abc" reverse?)'))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual(result.head.value, "cba")

    def test_reverse_link_ok_tagged(self):
        expr, _ = parse(tokenize("('(1 2 3) reverse?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "ok"))
        self.assertEqual([e.value for e in _collect_link(result.head)], [3, 2, 1])

    def test_reverse_err_tagged(self):
        expr, _ = parse(tokenize("(42 reverse?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "reverse of int")

    def test_reverse_underflow_err_tagged(self):
        expr, _ = parse(tokenize("(reverse?)"))
        result = self.machine.run(expr=expr)
        self.assertTrue(_is_tagged(result, "err"))
        self.assertEqual(result.head.value, "stack underflow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
