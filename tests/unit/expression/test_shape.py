import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import (  # noqa: E402
    Expr,
    NIL,
    ZERO32,
    bytes_,
    exprs_to_linked_expr,
    int_,
    link,
    symbol,
)
from astreum.expression.expr import (  # noqa: E402
    _BUILTIN_TYPE_HASH,
    get_expr_head_hash,
    get_expr_tail_hash,
)
from astreum.expression.helpers import builtin_composite_payload  # noqa: E402
from astreum.expression.shape import (  # noqa: E402
    HOLD,
    INT,
    MAX_SHAPE_DEPTH,
    SEND,
    SHAPE_HOLD,
    SHAPE_PAIR,
    SHAPE_REPEAT,
    SHAPE_SEND,
    pair,
    parse_shape,
    repeat,
    walk_shape,
)


def make_store(root: Expr) -> dict[bytes, Expr]:
    """Return {hash: shallow expr} for every expr below root, as cold storage keeps it."""
    store: dict[bytes, Expr] = {}

    def add(expr):
        if expr is None or expr is NIL or expr.hash() in store:
            return
        if expr.base != "link":
            store[expr.hash()] = expr
            return
        store[expr.hash()] = Expr(
            "link",
            head_hash=get_expr_head_hash(expr),
            tail_hash=get_expr_tail_hash(expr),
        )
        payload = builtin_composite_payload(expr)
        if payload is not None:
            add(payload)
            return
        add(expr.head)
        if expr.tail is not None and expr.tail.hash() in _BUILTIN_TYPE_HASH:
            return
        add(expr.tail)

    add(root)
    return store


class TestParseShape(unittest.TestCase):
    def test_parse_simple_rules(self):
        self.assertEqual(parse_shape(HOLD), (SHAPE_HOLD,))
        self.assertEqual(parse_shape(SEND), (SHAPE_SEND,))
        self.assertEqual(parse_shape(repeat(HOLD)), (SHAPE_REPEAT, (SHAPE_HOLD,)))
        self.assertEqual(
            parse_shape(pair(SEND, HOLD)), (SHAPE_PAIR, (SHAPE_SEND,), (SHAPE_HOLD,))
        )

    def test_int_is_pair_send_hold(self):
        self.assertEqual(INT, bytes([2, 1, 0]))

    def test_rejects_bad_shapes(self):
        for bad in (b"", b"\x09", b"\x02\x01", b"\x03", b"\x01\x01", b"\x02\x01\x00\x00"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_shape(bad)

    def test_rejects_shape_deeper_than_the_limit(self):
        parse_shape(b"\x03" * (MAX_SHAPE_DEPTH - 1) + HOLD)
        with self.assertRaises(ValueError):
            parse_shape(b"\x03" * MAX_SHAPE_DEPTH + HOLD)


class TestWalkShape(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.c = bytes_(b"a"), bytes_(b"b"), bytes_(b"c")
        self.lst = exprs_to_linked_expr([self.a, self.b, self.c])
        self.store = make_store(self.lst)

    def walk(self, shape, root=None, store=None):
        store = self.store if store is None else store
        root = self.lst if root is None else root
        return walk_shape(parse_shape(shape), root.hash(), store.get)

    def test_hold_sends_nothing(self):
        found, missing = self.walk(HOLD)
        self.assertEqual((found, missing), ([], []))

    def test_send_sends_only_the_expr(self):
        found, missing = self.walk(SEND)
        self.assertEqual([e.hash() for e in found], [self.lst.hash()])
        self.assertEqual(missing, [])

    def test_repeat_hold_sends_the_tail_chain_and_no_heads(self):
        found, _ = self.walk(repeat(HOLD))
        self.assertEqual(len(found), 3)
        self.assertNotIn(self.a.hash(), [e.hash() for e in found])

    def test_repeat_send_sends_chain_and_heads(self):
        found, missing = self.walk(repeat(SEND))
        hashes = {e.hash() for e in found}
        self.assertEqual(len(found), 6)
        self.assertEqual(missing, [])
        for item in (self.a, self.b, self.c):
            self.assertIn(item.hash(), hashes)

    def test_root_comes_first(self):
        found, _ = self.walk(repeat(SEND))
        self.assertEqual(found[0].hash(), self.lst.hash())

    def test_pair_sends_head_and_keeps_tail_as_hash(self):
        found, _ = self.walk(pair(SEND, HOLD))
        self.assertEqual(
            {e.hash() for e in found}, {self.lst.hash(), self.a.hash()}
        )

    def test_pair_can_skip_head_and_follow_tail(self):
        found, _ = self.walk(pair(HOLD, pair(SEND, HOLD)))
        second = self.lst.tail
        self.assertEqual(
            {e.hash() for e in found}, {self.lst.hash(), second.hash(), self.b.hash()}
        )

    def test_int_sends_the_int_and_its_payload_only(self):
        value = int_(7)
        store = make_store(value)
        found, missing = walk_shape(parse_shape(INT), value.hash(), store.get)
        self.assertEqual(len(found), 2)
        self.assertEqual(missing, [])
        self.assertEqual(found[1].value, b"\x07")
        self.assertEqual(found[1].base, "bytes")

    def test_missing_child_is_reported_and_not_walked_below(self):
        store = dict(self.store)
        del store[self.lst.tail.hash()]
        found, missing = self.walk(repeat(SEND), store=store)
        self.assertEqual(missing, [self.lst.tail.hash()])
        self.assertEqual(
            {e.hash() for e in found}, {self.lst.hash(), self.a.hash()}
        )

    def test_missing_root(self):
        found, missing = self.walk(SEND, store={})
        self.assertEqual((found, missing), ([], [self.lst.hash()]))

    def test_nil_and_zero_hash_are_skipped(self):
        found, missing = walk_shape(parse_shape(repeat(SEND)), ZERO32, {}.get)
        self.assertEqual((found, missing), ([], []))

    def test_rule_on_a_non_link_is_send(self):
        found, _ = walk_shape(
            parse_shape(pair(SEND, SEND)), self.a.hash(), self.store.get
        )
        self.assertEqual([e.hash() for e in found], [self.a.hash()])

    def test_nested_repeat_walks_a_list_of_lists(self):
        inner1 = exprs_to_linked_expr([int_(1), int_(2)])
        inner2 = exprs_to_linked_expr([int_(3), int_(4)])
        outer = exprs_to_linked_expr([inner1, inner2])
        store = make_store(outer)
        found, missing = walk_shape(
            parse_shape(repeat(repeat(INT))), outer.hash(), store.get
        )
        self.assertEqual(missing, [])
        # 2 outer + 4 inner + 4 ints + 4 payloads
        self.assertEqual(len(found), 14)

    def test_an_expr_is_sent_once(self):
        same = exprs_to_linked_expr([self.a, self.a])
        store = make_store(same)
        found, _ = walk_shape(parse_shape(repeat(SEND)), same.hash(), store.get)
        hashes = [e.hash() for e in found]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_symbol_tail_is_sent_by_pair(self):
        tagged = link(link(self.a, None), symbol("block"))
        store = make_store(tagged)
        found, missing = walk_shape(
            parse_shape(pair(SEND, SEND)), tagged.hash(), store.get
        )
        self.assertEqual(missing, [])
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
