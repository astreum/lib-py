import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "src")

from astreum.expression import bytes_
from astreum.expression.encoding import encode_expr_to_bytes
from astreum.storage.cold import put_expr_in_cold_storage
from astreum.storage.records import put_record_in_cold_storage
from astreum.storage.setup import setup_storage
from astreum.utils.config import config_setup


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


def _make_node(cold_path: str) -> SimpleNamespace:
    config = config_setup(
        {
            "default_seed": None,
            "cold_storage_path": cold_path,
            "cold_storage_scale": "KB",
        }
    )
    node = SimpleNamespace(logger=_silent_logger(), config=config)
    setup_storage(node, config)
    return node


def _disk_size(cold_path: str) -> int:
    total = 0
    for table in ("exprs", "records"):
        for level_path in (Path(cold_path) / table).glob("level_*"):
            for entry in level_path.iterdir():
                if entry.is_file():
                    total += entry.stat().st_size
    return total


class TestColdStorageSize(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cold_path = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_empty_store_starts_at_zero(self) -> None:
        node = _make_node(self.cold_path)
        self.assertEqual(node.cold_storage_size, 0)

    def test_expr_put_adds_size(self) -> None:
        node = _make_node(self.cold_path)
        expr = bytes_(b"hello")
        self.assertTrue(put_expr_in_cold_storage(node, expr))
        self.assertEqual(node.cold_storage_size, len(encode_expr_to_bytes(expr)))

    def test_record_put_adds_size(self) -> None:
        node = _make_node(self.cold_path)
        slot_ids = [b"\x01" * 32, b"\x02" * 32]
        self.assertTrue(put_record_in_cold_storage(node, b"\xaa" * 32, slot_ids))
        stored = bytes_(b"".join(slot_ids))
        self.assertEqual(node.cold_storage_size, len(encode_expr_to_bytes(stored)))

    def test_startup_counts_all_levels_of_both_tables(self) -> None:
        node = _make_node(self.cold_path)
        for value in range(64):
            put_expr_in_cold_storage(node, bytes_(value.to_bytes(64, "big")))
        put_record_in_cold_storage(node, b"\xbb" * 32, [b"\x03" * 32])
        self.assertTrue(
            any((Path(self.cold_path) / "exprs" / "level_1").glob("*_data"))
            or any((Path(self.cold_path) / "exprs" / "level_2").glob("*_data")),
            "expected collated levels on disk",
        )

        restarted = _make_node(self.cold_path)
        self.assertEqual(restarted.cold_storage_size, _disk_size(self.cold_path))


if __name__ == "__main__":
    unittest.main()
