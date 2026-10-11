"""get_expr_builtin_tag returns a builtin tag with no I/O, else "link"."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import (  # noqa: E402
    Expr,
    HASH_SYMBOL_INT,
    HASH_SYMBOL_STR,
    NIL,
    ZERO32,
    bytes_,
    fp64_,
    get_expr_builtin_tag,
    int_,
    link,
    str_,
    symbol,
)


class TestGetExprBuiltinTag(unittest.TestCase):
    def test_terminals(self) -> None:
        self.assertEqual(get_expr_builtin_tag(symbol("x")), "symbol")
        self.assertEqual(get_expr_builtin_tag(bytes_(b"ab")), "bytes")

    def test_builtin_composites(self) -> None:
        self.assertEqual(get_expr_builtin_tag(int_(5)), "int")
        self.assertEqual(get_expr_builtin_tag(str_("hi")), "str")
        self.assertEqual(get_expr_builtin_tag(fp64_(1.5)), "fp64")

    def test_plain_link(self) -> None:
        self.assertEqual(get_expr_builtin_tag(link(int_(1), int_(2))), "link")
        self.assertEqual(get_expr_builtin_tag(NIL), "link")

    def test_custom_symbol_tail_is_link(self) -> None:
        tagged = link(bytes_(b"x"), symbol("block"))
        self.assertEqual(get_expr_builtin_tag(tagged), "link")

    def test_unresolved_builtin_tail_hash(self) -> None:
        self.assertEqual(
            get_expr_builtin_tag(Expr("link", head_hash=ZERO32, tail_hash=HASH_SYMBOL_INT)),
            "int",
        )
        self.assertEqual(
            get_expr_builtin_tag(Expr("link", head_hash=ZERO32, tail_hash=HASH_SYMBOL_STR)),
            "str",
        )

    def test_unresolved_custom_tail_hash_is_link(self) -> None:
        self.assertEqual(
            get_expr_builtin_tag(Expr("link", head_hash=ZERO32, tail_hash=b"\x07" * 32)),
            "link",
        )

    def test_unresolved_terminal_type_hash_is_link(self) -> None:
        """A tail hash for "symbol" is not a composite type, so it stays a link."""
        from astreum.expression import HASH_SYMBOL_SYMBOL

        self.assertEqual(
            get_expr_builtin_tag(Expr("link", head_hash=ZERO32, tail_hash=HASH_SYMBOL_SYMBOL)),
            "link",
        )


if __name__ == "__main__":
    unittest.main()
