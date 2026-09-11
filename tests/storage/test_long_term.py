import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.expression import Expr, NIL, ZERO32, bytes_, int_, link
from astreum.consensus.transaction.storage.model import StorageSlot
from astreum.utils.config import (
    DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS,
    config_setup,
)
from astreum.storage.cold import get_expr_from_cold_storage
from astreum.storage.records import (
    fetch_and_store_record,
    get_record_from_cold_storage,
)
from astreum.storage.setup import setup_storage
from astreum.node import Node


def _make_node(cold_path: str | None) -> Node:
    config = {
        "cold_storage_scale": "KB",
        "cold_storage_base_size": 10 * 1024 * 1024,
        "default_seed": None,
        "verbose": False,
    }
    if cold_path is not None:
        config["cold_storage_path"] = cold_path
    return Node(config)


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


class TestLongTermConfig(unittest.TestCase):
    def test_default_off_and_interval_default(self):
        config = config_setup({"default_seed": None})
        self.assertFalse(config["long_term_storage"])
        self.assertEqual(
            config["long_term_storage_interval"],
            DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS,
        )

    def test_on_when_set(self):
        config = config_setup({"default_seed": None, "long_term_storage": True})
        self.assertTrue(config["long_term_storage"])

    def test_interval_parsed(self):
        config = config_setup(
            {"default_seed": None, "long_term_storage_interval": "2.5"}
        )
        self.assertEqual(config["long_term_storage_interval"], 2.5)

    def test_interval_must_be_number(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "long_term_storage_interval": "abc"})

    def test_interval_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                config_setup({"default_seed": None, "long_term_storage_interval": bad})

    def test_must_be_boolean(self):
        with self.assertRaises(ValueError):
            config_setup({"default_seed": None, "long_term_storage": "yes"})


class TestLongTermSetup(unittest.TestCase):
    def test_disabled_without_cold_path(self):
        config = config_setup({"default_seed": None, "long_term_storage": True})
        node = SimpleNamespace(logger=_silent_logger())
        setup_storage(node, config)
        self.assertFalse(node.long_term_storage)
        self.assertEqual(node.long_term_cursor, 0)
        self.assertEqual(
            node.long_term_storage_interval,
            DEFAULT_LONG_TERM_STORAGE_INTERVAL_SECONDS,
        )

    def test_enabled_with_cold_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = config_setup(
                {
                    "default_seed": None,
                    "long_term_storage": True,
                    "long_term_storage_interval": 3.0,
                    "cold_storage_path": temp_dir,
                }
            )
            node = SimpleNamespace(logger=_silent_logger())
            setup_storage(node, config)
            self.assertTrue(node.long_term_storage)
            self.assertEqual(node.long_term_storage_interval, 3.0)
            self.assertEqual(node.long_term_cursor, 0)


class TestFetchAndStoreRecord(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = _make_node(self.temp_dir.name)
        self.storage_id = b"\x0a" * 32
        self.leaf_a = link(int_(1), NIL)
        self.leaf_b = link(int_(2), NIL)
        self.other = link(int_(3), NIL)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _shallow_root(self, head, tail):
        return Expr("link", head_hash=head.hash(), tail_hash=tail.hash())

    def _run(self, root, trie_values, new_count):
        exprs = {
            self.storage_id: root,
            self.leaf_a.hash(): self.leaf_a,
            self.leaf_b.hash(): self.leaf_b,
            self.other.hash(): self.other,
        }
        with patch(
            "astreum.storage.records.fetch.get_expr_from_local_storage",
            side_effect=lambda n, h: exprs.get(h),
        ), patch(
            "astreum.storage.records.fetch.get_from_radix_tree",
            side_effect=lambda tree, n, h: trie_values.get(h),
        ):
            return fetch_and_store_record(
                self.node, self.storage_id, SimpleNamespace(), new_count
            )

    def _slot(self, storage_id, sequence):
        return StorageSlot(storage_id=storage_id, sequence=sequence).expr()

    def test_slots_written_in_sequence_order(self):
        root = self._shallow_root(self.leaf_a, self.leaf_b)
        trie = {
            self.leaf_a.hash(): self._slot(self.storage_id, 0),
            self.leaf_b.hash(): self._slot(self.storage_id, 1),
        }
        self.assertTrue(self._run(root, trie, 2))
        self.assertEqual(
            get_record_from_cold_storage(self.node, self.storage_id),
            self.leaf_a.hash() + self.leaf_b.hash(),
        )

    def test_cold_files_written_for_root_and_subexprs(self):
        root = self._shallow_root(self.leaf_a, self.leaf_b)
        trie = {
            self.leaf_a.hash(): self._slot(self.storage_id, 0),
            self.leaf_b.hash(): self._slot(self.storage_id, 1),
        }
        self.assertTrue(self._run(root, trie, 2))
        for expr in (root, self.leaf_a, self.leaf_b):
            self.assertIsNotNone(
                get_expr_from_cold_storage(self.node, expr.hash())
            )

    def test_shared_ref_skipped_from_slot_list(self):
        # `other` is slotted under a different record: excluded from the slot
        # list and its subtree is not walked.
        root = self._shallow_root(self.leaf_a, self.other)
        trie = {
            self.leaf_a.hash(): self._slot(self.storage_id, 0),
            self.other.hash(): self._slot(b"\x99" * 32, 0),
        }
        self.assertTrue(self._run(root, trie, 2))
        self.assertEqual(
            get_record_from_cold_storage(self.node, self.storage_id),
            self.leaf_a.hash() + ZERO32,
        )
        self.assertIsNone(get_expr_from_cold_storage(self.node, self.other.hash()))

    def test_fetch_failure_writes_nothing(self):
        with patch(
            "astreum.storage.records.fetch.get_expr_from_local_storage",
            return_value=None,
        ), patch(
            "astreum.storage.exprs.network.get_expr_from_network",
            return_value=None,
        ):
            result = fetch_and_store_record(
                self.node, self.storage_id, SimpleNamespace(), 2
            )
        self.assertFalse(result)
        self.assertIsNone(get_record_from_cold_storage(self.node, self.storage_id))

    def test_idempotent_when_everything_local(self):
        root = self._shallow_root(self.leaf_a, self.leaf_b)
        trie = {
            self.leaf_a.hash(): self._slot(self.storage_id, 0),
            self.leaf_b.hash(): self._slot(self.storage_id, 1),
        }
        self.assertTrue(self._run(root, trie, 2))
        first = get_record_from_cold_storage(self.node, self.storage_id)
        self.assertTrue(self._run(root, trie, 2))
        self.assertEqual(get_record_from_cold_storage(self.node, self.storage_id), first)


REC_HASH = b"\x0b" * 32


def _lt_node(**overrides):
    node = SimpleNamespace(
        long_term_storage=True,
        long_term_cursor=0,
        storage_index={REC_HASH: 0},
        latest_block=None,
        logger=_silent_logger(),
    )
    for key, value in overrides.items():
        setattr(node, key, value)
    return node


def _latest_block(account=None):
    return SimpleNamespace(
        accounts=SimpleNamespace(
            get_account=lambda address, node: account
        )
    )


class TestLongTermStoreOne(unittest.TestCase):
    def _patch_fetch(self, result=True):
        fetch = MagicMock(return_value=result)
        patcher = patch(
            "astreum.storage.workers.advertisements.fetch_and_store_record",
            fetch,
        )
        return patcher, fetch

    def test_disabled_noop(self):
        node = _lt_node(long_term_storage=False)
        self.assertFalse(_long_term_store_one(node))
        self.assertEqual(node.long_term_cursor, 0)

    def test_empty_index_noop(self):
        node = _lt_node(storage_index={})
        self.assertFalse(_long_term_store_one(node))
        self.assertEqual(node.long_term_cursor, 0)

    def test_already_in_records_table_no_fetch(self):
        node = _lt_node()
        with patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=b"\x00" * 64,
        ) as mock_value, patch(
            "astreum.storage.workers.advertisements.fetch_and_store_record"
        ) as mock_fetch:
            self.assertFalse(_long_term_store_one(node))
        mock_value.assert_called_once()
        mock_fetch.assert_not_called()
        self.assertEqual(node.long_term_cursor, 1)

    def test_no_latest_block_noop(self):
        node = _lt_node()
        patcher, fetch = self._patch_fetch()
        with patcher, patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=None,
        ):
            self.assertFalse(_long_term_store_one(node))
        fetch.assert_not_called()

    def test_no_storage_account_noop(self):
        node = _lt_node(latest_block=_latest_block(account=None))
        patcher, fetch = self._patch_fetch()
        with patcher, patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=None,
        ):
            self.assertFalse(_long_term_store_one(node))
        fetch.assert_not_called()

    def test_header_not_in_trie_noop(self):
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x01" * 32))
        node = _lt_node(latest_block=_latest_block(account=account))
        patcher, fetch = self._patch_fetch()
        with patcher, patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=None,
        ), patch(
            "astreum.storage.workers.advertisements.get_from_radix_tree",
            return_value=None,
        ):
            self.assertFalse(_long_term_store_one(node))
        fetch.assert_not_called()

    def test_happy_path_fetches_and_stores(self):
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x01" * 32))
        node = _lt_node(latest_block=_latest_block(account=account))
        patcher, fetch = self._patch_fetch(result=True)
        with patcher, patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=None,
        ), patch(
            "astreum.storage.workers.advertisements.get_from_radix_tree",
            return_value=object(),  # record header sentinel
        ), patch(
            "astreum.storage.workers.advertisements.parse_record_new_count",
            return_value=3,
        ) as mock_count:
            self.assertTrue(_long_term_store_one(node))
        mock_count.assert_called_once()
        fetch.assert_called_once()
        args = fetch.call_args.args
        self.assertEqual(args[0], node)
        self.assertEqual(args[1], REC_HASH)
        self.assertIsInstance(args[2], RadixTree)
        self.assertEqual(args[2].root_hash, b"\x01" * 32)
        self.assertEqual(args[3], 3)
        self.assertEqual(node.long_term_cursor, 1)


# Import after patch-target definitions so module-level names resolve the
# same way the tests patch them.
from astreum.storage.workers.advertisements import (  # noqa: E402
    _long_term_store_one,
    advertise_storage,
)
from astreum.storage.radix import RadixTree  # noqa: E402


class TestAdvertiseStorageLoop(unittest.TestCase):
    def _run_loop(self, node, duration=0.4):
        thread = threading.Thread(target=advertise_storage, args=(node,), daemon=True)
        thread.start()
        time.sleep(duration)
        node.communication_stop_event.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def _loop_node(self, long_term, interval=0.05):
        return SimpleNamespace(
            config={"storage_request_price_interval": interval},
            long_term_storage=long_term,
            long_term_storage_interval=interval,
            communication_stop_event=threading.Event(),
            logger=_silent_logger(),
        )

    def test_long_term_off_price_only(self):
        node = self._loop_node(long_term=False)
        with patch(
            "astreum.storage.workers.advertisements._update_storage_request_price"
        ) as mock_price, patch(
            "astreum.storage.workers.advertisements._long_term_store_one"
        ) as mock_lt:
            self._run_loop(node)
        self.assertGreaterEqual(mock_price.call_count, 1)
        mock_lt.assert_not_called()

    def test_long_term_on_invoked_on_cadence(self):
        node = self._loop_node(long_term=True)
        with patch(
            "astreum.storage.workers.advertisements._update_storage_request_price"
        ), patch(
            "astreum.storage.workers.advertisements._long_term_store_one"
        ) as mock_lt:
            self._run_loop(node)
        self.assertGreaterEqual(mock_lt.call_count, 1)

    def test_step_exception_does_not_kill_loop(self):
        node = self._loop_node(long_term=True)
        with patch(
            "astreum.storage.workers.advertisements._update_storage_request_price"
        ), patch(
            "astreum.storage.workers.advertisements._long_term_store_one",
            side_effect=RuntimeError("boom"),
        ):
            self._run_loop(node, duration=0.3)


if __name__ == "__main__":
    unittest.main()
