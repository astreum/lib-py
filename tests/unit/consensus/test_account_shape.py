"""``ACCOUNT_SHAPE`` and ``STORAGE_RECORD_SHAPE`` reach every expr their readers need."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.account import create_account  # noqa: E402
from astreum.consensus.account.model import ACCOUNT_SHAPE  # noqa: E402
from astreum.consensus.constants import PROVIDERS_PER_RECORD  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.consensus.transaction.storage.model import (  # noqa: E402
    STORAGE_RECORD_SHAPE,
    StorageLane,
    StorageRecord,
    storage_record_cells,
    storage_record_from_expr,
    storage_record_to_expr,
)
from astreum.expression import resolve_inner_exprs  # noqa: E402
from astreum.expression.encoding import decode_expr_from_bytes, encode_expr_to_bytes  # noqa: E402
from astreum.expression.shape import parse_shape, walk_shape  # noqa: E402
from astreum.storage.radix import (  # noqa: E402
    RadixTree,
    get_radix_node_expr,
    put_in_radix_tree,
)
from astreum.storage.radix.tree.walk import collect_radix_all  # noqa: E402
from tests.unit.storage.test_contact import _put, _store_node  # noqa: E402

BLOCK = b"\x0b" * 32
DATA = b"\x11" * 32
CODE = b"\x22" * 32
CHANNELS = b"\x33" * 32
ADDRESS = b"\xa1" * 32


def _reader_from(exprs):
    store = _store_node()
    store.is_connected = False
    for expr in exprs:
        _put(store, decode_expr_from_bytes(encode_expr_to_bytes(expr)))
    return store


class TestAccountShape(unittest.TestCase):
    def _accounts_root(self, source, account):
        tree = RadixTree()
        put_in_radix_tree(tree, source, ADDRESS, account.to_expr())
        for node_hash, node in tree.nodes.items():
            expr = get_radix_node_expr(node)
            for item in resolve_inner_exprs(source, expr)[0]:
                _put(source, item)
            if node.value is not None and not isinstance(node.value, bytes):
                for item in resolve_inner_exprs(source, node.value)[0]:
                    _put(source, item)
        return tree.root_hash

    def _read(self, account):
        source = _store_node()
        source.is_connected = False
        root = self._accounts_root(source, account)
        exprs, missing = collect_radix_all(source, root, None, ACCOUNT_SHAPE)
        self.assertEqual(missing, [])
        reader = _reader_from(exprs)
        return Accounts(root_hash=root).get_account(ADDRESS, reader)

    def test_the_shape_is_valid(self):
        parse_shape(ACCOUNT_SHAPE)

    def test_an_account_reads_from_the_exprs_alone(self):
        for data_block, code_block, channels_block in (
            (None, None, None),
            (BLOCK, BLOCK, BLOCK),
            (BLOCK, None, BLOCK),
        ):
            with self.subTest(blocks=(data_block, code_block, channels_block)):
                account = create_account(
                    balance=1_000_000,
                    data_hash=DATA,
                    code_hash=CODE,
                    channels_hash=CHANNELS,
                    counter=9,
                    data_block=data_block,
                    code_block=code_block,
                    channels_block=channels_block,
                )
                got = self._read(account)
                self.assertIsNotNone(got)
                self.assertEqual(got.state_key(), account.state_key())
                self.assertEqual(
                    (got.data_block, got.code_block, got.channels_block),
                    (data_block, code_block, channels_block),
                )

    def test_an_account_with_empty_roots_reads_from_the_exprs_alone(self):
        account = create_account(balance=42)
        got = self._read(account)
        self.assertIsNotNone(got)
        self.assertEqual(got.state_key(), account.state_key())

    def test_the_shape_does_not_enter_the_pointed_tries(self):
        account = create_account(
            balance=1, data_hash=DATA, code_hash=CODE, channels_hash=CHANNELS,
            data_block=BLOCK, code_block=BLOCK, channels_block=BLOCK,
        )
        expr = account.to_expr()
        source = _store_node()
        source.is_connected = False
        for item in resolve_inner_exprs(source, expr)[0]:
            _put(source, item)
        reached, missing = walk_shape(parse_shape(ACCOUNT_SHAPE), expr.hash(), source.hot_storage.get)
        self.assertFalse({DATA, CODE, CHANNELS} & {e.hash() for e in reached})
        self.assertLessEqual(set(missing), {DATA, CODE, CHANNELS, BLOCK})


def _record(same_lanes: bool = False) -> StorageRecord:
    lanes = tuple(
        StorageLane(
            block_hash=BLOCK,
            height=5 if same_lanes else 5 + i,
            winner=b"\x44" * 32 if same_lanes else bytes([0x40 + i]) * 32,
        )
        for i in range(PROVIDERS_PER_RECORD)
    )
    return StorageRecord(
        creation_block_hash=BLOCK,
        lanes=lanes,
        new_size=1234,
        new_count=7,
        slots_root=b"\x55" * 32,
    )


class TestStorageRecordShape(unittest.TestCase):
    def _reached(self, record):
        expr = storage_record_to_expr(record)
        source = _store_node()
        source.is_connected = False
        for item in resolve_inner_exprs(source, expr)[0]:
            _put(source, item)
        return walk_shape(parse_shape(STORAGE_RECORD_SHAPE), expr.hash(), source.hot_storage.get)

    def test_the_shape_is_valid(self):
        parse_shape(STORAGE_RECORD_SHAPE)

    def test_a_record_reads_from_the_exprs_alone(self):
        for same in (False, True):
            with self.subTest(same_lanes=same):
                record = _record(same)
                exprs, missing = self._reached(record)
                self.assertLessEqual(set(missing), {BLOCK})
                reader = _reader_from(exprs)
                got = storage_record_from_expr(reader, storage_record_to_expr(record))
                self.assertEqual(got.lanes, record.lanes)
                self.assertEqual(got.slots_root, record.slots_root)
                self.assertEqual(
                    (got.new_count, got.new_size, got.creation_block_hash),
                    (7, 1234, BLOCK),
                )

    def test_the_shape_reaches_the_cells_the_record_holds(self):
        record = _record()
        exprs, _ = self._reached(record)
        reached = {e.hash() for e in exprs}
        self.assertTrue({e.hash() for e in storage_record_cells(record)} <= reached)

    def test_equal_lanes_appear_once(self):
        exprs, _ = self._reached(_record(same_lanes=True))
        hashes = [e.hash() for e in exprs]
        self.assertEqual(len(hashes), len(set(hashes)))


if __name__ == "__main__":
    unittest.main()
