from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import bytes_
from astreum.node import Node
from astreum.storage.cold import (
    get_expr_from_cold_storage,
    get_exprs_from_cold_storage,
    iter_exprs_in_cold_storage,
    put_expr_in_cold_storage,
)
from astreum.storage.records import (
    get_records_from_cold_storage,
    iter_records_in_cold_storage,
    put_record_in_cold_storage,
)


class _CountingLock:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.acquires = 0

    def __enter__(self):
        self.acquires += 1
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)


class TestColdBatch(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = Node(
            {
                "cold_storage_path": self.temp_dir.name,
                "cold_storage_scale": "KB",
                "is_storage_provider": True,
                "default_seed": None,
                "verbose": False,
            }
        )
        self.root = Path(self.temp_dir.name)
        self.ids: list[bytes] = []
        value = 1
        level_1 = self.root / "exprs" / "level_1"
        while not (level_1.exists() and any(level_1.glob("*_index"))):
            self._put(value)
            value += 1
            self.assertLess(value, 200, "cold storage did not compact to level_1")
        for _ in range(5):
            self._put(value)
            value += 1

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _put(self, value: int) -> None:
        expr = bytes_(value.to_bytes(64, "big", signed=False))
        self.assertTrue(put_expr_in_cold_storage(self.node, expr))
        self.ids.append(expr.hash())

    def test_batch_get_matches_single_get(self) -> None:
        level_0 = {p.stem for p in (self.root / "exprs" / "level_0").glob("*.bin")}
        self.assertTrue(level_0)
        self.assertTrue(any(i.hex().upper() not in level_0 for i in self.ids))
        missing = b"\x00" * 32
        keys = self.ids + [missing]
        batch = get_exprs_from_cold_storage(self.node, keys)
        self.assertNotIn(missing, batch)
        self.assertEqual(set(batch), set(self.ids))
        for key in self.ids:
            single = get_expr_from_cold_storage(self.node, key)
            self.assertEqual(batch[key].value, single.value)

    def test_batch_get_takes_lock_once(self) -> None:
        counter = _CountingLock(self.node.cold_storage_lock)
        self.node.cold_storage_lock = counter
        result = get_exprs_from_cold_storage(self.node, self.ids[:10])
        self.assertEqual(len(result), 10)
        self.assertEqual(counter.acquires, 1)

    def test_batch_get_records(self) -> None:
        a, b = b"\x01" * 32, b"\x02" * 32
        slots_a = [b"\xaa" * 32, b"\xab" * 32]
        slots_b = [b"\xba" * 32, b"\xbb" * 32]
        self.assertTrue(put_record_in_cold_storage(self.node, a, slots_a))
        self.assertTrue(put_record_in_cold_storage(self.node, b, slots_b))
        result = get_records_from_cold_storage(self.node, [a, b, b"\x03" * 32])
        self.assertEqual(result, {a: b"".join(slots_a), b: b"".join(slots_b)})

    def test_put_record_appends_new_id(self) -> None:
        key = b"\x05" * 32
        self.assertEqual(len(self.node.new_record_ids), 0)
        self.assertTrue(put_record_in_cold_storage(self.node, key, [b"\xdd" * 32]))
        self.assertEqual(list(self.node.new_record_ids), [key])

    def test_put_record_on_a_non_provider_node_keeps_no_new_ids(self) -> None:
        with tempfile.TemporaryDirectory() as other_dir:
            node = Node(
                {
                    "cold_storage_path": other_dir,
                    "cold_storage_scale": "KB",
                    "default_seed": None,
                    "verbose": False,
                }
            )
            self.assertFalse(node.is_storage_provider)
            self.assertIsNone(node.new_record_ids)
            self.assertTrue(put_record_in_cold_storage(node, b"\x06" * 32, [b"\xdd" * 32]))

    def test_batched_iter_same_ids(self) -> None:
        plain = list(iter_exprs_in_cold_storage(self.node))
        batched = list(iter_exprs_in_cold_storage(self.node, batch=3))
        self.assertEqual(len(batched), len(set(batched)))
        self.assertEqual(set(batched), set(plain))
        self.assertEqual(set(batched), set(self.ids))

    def test_batched_iter_records(self) -> None:
        keys = [bytes([i]) * 32 for i in range(1, 4)]
        for key in keys:
            self.assertTrue(put_record_in_cold_storage(self.node, key, [b"\xcc" * 32]))
        self.assertEqual(set(iter_records_in_cold_storage(self.node, batch=2)), set(keys))

    def test_batched_iter_survives_removed_index(self) -> None:
        level_1 = self.root / "exprs" / "level_1"
        index_files = sorted(level_1.glob("*_index"))
        self.assertTrue(index_files)
        gen = iter_exprs_in_cold_storage(self.node, batch=1)
        seen: list[bytes] = []
        level_0_count = len(list((self.root / "exprs" / "level_0").glob("*.bin")))
        for _ in range(level_0_count + 1):
            seen.append(next(gen))
        for path in index_files:
            path.unlink()
        seen.extend(gen)
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
