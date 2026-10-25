"""``USER_RECORD_SHAPE`` reaches every expr a stake record reader needs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.transaction.treasury.record import (  # noqa: E402
    USER_RECORD_SHAPE,
    TreasuryUserRecord,
    _root_item,
)
from astreum.expression import NIL, int_, link, resolve_inner_exprs  # noqa: E402
from astreum.expression.encoding import (  # noqa: E402
    decode_expr_from_bytes,
    encode_expr_to_bytes,
)
from astreum.expression.shape import parse_shape, walk_shape  # noqa: E402
from tests.unit.storage.test_contact import _put, _store_node  # noqa: E402

BLOCK = b"\x0b" * 32
LOANS = b"\x11" * 32
GUARANTEES = b"\x22" * 32


def _four_field_record(loans_root: bytes, guarantees_root: bytes) -> TreasuryUserRecord:
    """A record as it was before the unsecured loan fields."""
    expr = link(
        int_(500),
        link(
            _root_item(loans_root, None),
            link(int_(7), link(_root_item(guarantees_root, None), NIL)),
        ),
    )
    return expr


class TestUserRecordShape(unittest.TestCase):
    def _reader_store(self, record_expr):
        """Return a store that holds only what the shape reaches from the record."""
        source = _store_node()
        # The trie roots are only hashes here. Nothing is fetched for them.
        source.is_connected = False
        for item in resolve_inner_exprs(source, record_expr)[0]:
            _put(source, item)
        exprs, missing = walk_shape(
            parse_shape(USER_RECORD_SHAPE), record_expr.hash(), source.hot_storage.get
        )
        # The roots of the tries are only hashes here, so they are not held.
        self.assertLessEqual(set(missing), {LOANS, GUARANTEES})
        store = _store_node()
        store.is_connected = False
        for expr in exprs:
            store.hot_storage[expr.hash()] = decode_expr_from_bytes(encode_expr_to_bytes(expr))
        return store

    def test_the_shape_is_valid(self):
        parse_shape(USER_RECORD_SHAPE)
        self.assertEqual(len(USER_RECORD_SHAPE), 37)

    def test_a_full_record_reads_from_the_exprs_alone(self):
        for loans_block, guarantees_block in ((None, None), (BLOCK, BLOCK), (None, BLOCK)):
            with self.subTest(loans_block=loans_block, guarantees_block=guarantees_block):
                record = TreasuryUserRecord(
                    balance=1_000_000,
                    loans_root_hash=LOANS,
                    total_interest_paid=12,
                    guarantees_root_hash=GUARANTEES,
                    loaned=3,
                    defaulted=4,
                    guaranteed=5,
                    loans_root_block=loans_block,
                    guarantees_root_block=guarantees_block,
                )
                store = self._reader_store(record.to_expr())
                got = TreasuryUserRecord.from_storage(store, record.to_expr().hash())
                self.assertEqual(got, record)
                self.assertEqual(got.loans_root_block, loans_block)
                self.assertEqual(got.guarantees_root_block, guarantees_block)

    def test_a_record_with_empty_roots_reads_from_the_exprs_alone(self):
        record = TreasuryUserRecord(balance=42)
        store = self._reader_store(record.to_expr())
        self.assertEqual(TreasuryUserRecord.from_storage(store, record.to_expr().hash()), record)

    def test_a_record_of_four_fields_reads_from_the_exprs_alone(self):
        expr = _four_field_record(LOANS, GUARANTEES)
        store = self._reader_store(expr)
        got = TreasuryUserRecord.from_storage(store, expr.hash())
        self.assertEqual(
            got,
            TreasuryUserRecord(
                balance=500, loans_root_hash=LOANS, total_interest_paid=7,
                guarantees_root_hash=GUARANTEES,
            ),
        )


if __name__ == "__main__":
    unittest.main()
