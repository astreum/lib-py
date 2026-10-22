import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import (
    Expr, int_, bytes_, symbol, link, get_expr_tag, get_expr_builtin_tag,
)
from astreum.expression.floats import (
    e4m3_, e5m2_, fp16_, bf16_, fp32_, fp64_,
)
from astreum.expression.encoding import encode_expr_to_bytes, decode_expr_from_bytes


def _roundtrip(expr: Expr) -> Expr:
    return decode_expr_from_bytes(encode_expr_to_bytes(expr))


class TestExprEncodeDecode(unittest.TestCase):

    def test_roundtrip_link(self):
        head = bytes_(b"hello")
        tail = int_(42)
        e = link(head, tail)
        r = _roundtrip(e)
        self.assertEqual(get_expr_builtin_tag(r), "link")
        self.assertEqual(r.head_hash, head.hash())
        self.assertEqual(r.tail_hash, tail.hash())

    def test_roundtrip_link_with_head_hash(self):
        hh = b"\xaa" * 32
        th = b"\xbb" * 32
        e = Expr("link", head_hash=hh, tail_hash=th)
        r = _roundtrip(e)
        self.assertEqual(get_expr_builtin_tag(r), "link")
        self.assertEqual(r.head_hash, hh)
        self.assertEqual(r.tail_hash, th)

    def test_roundtrip_int(self):
        e = int_(42)
        r = _roundtrip(e)
        self.assertEqual(get_expr_tag(r), "int")
        self.assertEqual(r.hash(), e.hash())

    def test_roundtrip_int_zero(self):
        e = int_(0)
        r = _roundtrip(e)
        self.assertEqual(get_expr_tag(r), "int")
        self.assertEqual(r.hash(), e.hash())

    def test_roundtrip_int_negative(self):
        e = int_(-7)
        r = _roundtrip(e)
        self.assertEqual(get_expr_tag(r), "int")
        self.assertEqual(r.hash(), e.hash())

    def test_roundtrip_bytes(self):
        e = bytes_(b"deadbeef")
        r = _roundtrip(e)
        self.assertEqual(get_expr_builtin_tag(r), "bytes")
        self.assertEqual(r.value, b"deadbeef")

    def test_roundtrip_bytes_empty(self):
        e = bytes_(b"")
        r = _roundtrip(e)
        self.assertEqual(get_expr_builtin_tag(r), "bytes")
        self.assertEqual(r.value, b"")

    def test_roundtrip_symbol(self):
        e = symbol("hello_world")
        r = _roundtrip(e)
        self.assertEqual(get_expr_builtin_tag(r), "symbol")
        self.assertEqual(r.value, "hello_world")

    def test_roundtrip_float_types_stored(self):
        for ctor, name in [
            (lambda: e4m3_(0.0), "e4m3"),
            (lambda: e5m2_(0.0), "e5m2"),
            (lambda: fp16_(0.0), "fp16"),
            (lambda: bf16_(0.0), "bf16"),
            (lambda: fp32_(0.0), "fp32"),
            (lambda: fp64_(0.0), "fp64"),
        ]:
            with self.subTest(name=name):
                e = ctor()
                encoded = encode_expr_to_bytes(e)
                self.assertIsNotNone(encoded, f"{name} encoded to None")
                self.assertIsInstance(encoded, bytes, f"{name} encoded to non-bytes")
                d = decode_expr_from_bytes(encoded)
                self.assertEqual(get_expr_tag(d), name, f"{name} decoded tag mismatch")

    def test_roundtrip_hash_stability(self):
        e = link(int_(1), int_(2))
        r = _roundtrip(e)
        self.assertEqual(r.hash(), e.hash())

    def test_roundtrip_deeply_nested_link(self):
        inner = int_(99)
        mid = link(inner, int_(0))
        outer = link(mid, int_(1))
        r = _roundtrip(outer)
        self.assertEqual(r.head_hash, mid.hash())
        self.assertEqual(r.tail_hash, int_(1).hash())


if __name__ == "__main__":
    unittest.main(verbosity=2)
