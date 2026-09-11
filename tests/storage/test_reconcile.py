import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "src")

from astreum.expression import Expr, NIL, ZERO32, bytes_, int_
from astreum.consensus.transaction.storage.model import StorageRecord, StorageSlot
from astreum.storage.cold import iter_exprs_in_cold_storage
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.storage.cold import collate_exprs
from astreum.storage.records import put_record_in_cold_storage, get_record_from_cold_storage, iter_records_in_cold_storage
from astreum.node import Node


def _make_node(cold_path: str) -> Node:
    node = Node(
        {
            "cold_storage_path": cold_path,
            "cold_storage_scale": "KB",
            "cold_storage_base_size": 10 * 1024 * 1024,
            "default_seed": None,
            "verbose": False,
        }
    )
    node.storage_public_key_bytes = b"\x02" * 32
    return node


def _store_expr(node: Node, expr: Expr) -> bytes:
    expr_id = expr.hash()
    stored = put_expr_in_cold_storage(node, expr)
    assert stored, f"failed to store expr {expr_id.hex()}"
    return expr_id


class TestIterExprsInColdStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = _make_node(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_cold_storage(self):
        self.assertEqual(list(iter_exprs_in_cold_storage(self.node)), [])

    def test_level_0_only(self):
        ids = {_store_expr(self.node, bytes_(f"v{i}".encode())) for i in range(3)}
        self.assertEqual(set(iter_exprs_in_cold_storage(self.node)), ids)

    def test_collated_level_1(self):
        exprs = [bytes_(f"v{i}".encode()) for i in range(3)]
        ids = {e.hash() for e in exprs}
        for e in exprs:
            _store_expr(self.node, e)
        self.assertTrue(collate_exprs(Path(self.node.config["cold_storage_path"]) / "exprs"))
        fresh_id = _store_expr(self.node, bytes_(b"fresh"))
        self.assertEqual(
            set(iter_exprs_in_cold_storage(self.node)), ids | {fresh_id}
        )

    def test_duplicate_across_levels(self):
        e = bytes_(b"dup")
        _store_expr(self.node, e)
        self.assertTrue(collate_exprs(Path(self.node.config["cold_storage_path"]) / "exprs"))
        _store_expr(self.node, e)
        ids = list(iter_exprs_in_cold_storage(self.node))
        self.assertIn(e.hash(), ids)


class TestRecordsTable(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = _make_node(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_write_and_read_roundtrip(self):
        storage_id = b"\xaa" * 32
        slot_ids = [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]
        self.assertTrue(put_record_in_cold_storage(self.node, storage_id, slot_ids))
        self.assertEqual(get_record_from_cold_storage(self.node, storage_id), b"".join(slot_ids))
        self.assertEqual(set(iter_records_in_cold_storage(self.node)), {storage_id})

    def test_write_collates_levels(self):
        storage_id = b"\xbb" * 32
        slot_ids = [b"\x01" * 32, b"\x02" * 32]
        self.assertTrue(put_record_in_cold_storage(self.node, storage_id, slot_ids))
        self.assertTrue(collate_exprs(Path(self.node.config["cold_storage_path"]) / "records"))
        self.assertEqual(get_record_from_cold_storage(self.node, storage_id), b"".join(slot_ids))
        self.assertEqual(set(iter_records_in_cold_storage(self.node)), {storage_id})


if __name__ == "__main__":
    unittest.main()
