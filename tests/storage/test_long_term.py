import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.expression import Expr, NIL, ZERO32, bytes_, int_, link
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.crypto.merkle import create_merkle_tree, generate_merkle_tree_proof, verify_merkle_tree_proof
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
    """The provider replays the validators' walk, cell by cell, and checks the
    record's root before it writes the records-table entry."""

    GET_EXPR = "astreum.consensus.transaction.storage.initial.get_expr"
    GET_BLOCK = "astreum.consensus.block.encoding.decode.get_block_from_storage"
    ROOT_GET = "astreum.storage.records.fetch.get_expr"
    TRIE = "astreum.consensus.transaction.storage.initial.get_from_radix_tree"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.node = _make_node(self.temp_dir.name)
        # A "block": a root whose cells the walk reaches only by hash.
        self.deep = link(int_(7), link(bytes_(b"deep"), NIL))
        self.leaf_a = link(int_(1), NIL)
        self.leaf_b = link(int_(2), NIL)
        whole = link(link(self.leaf_a, self.deep), link(self.leaf_b, NIL))
        self.storage_id = whole.hash()
        self.whole = whole

        # What the creator derives, holding the block whole.
        from tests.consensus.transaction.test_storage_record_refs import flat, store_flat

        self.flat, self.store_flat = flat, store_flat
        self.cells = {}
        self.record, self.slots = self._creator_record(whole)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _creator_record(self, whole):
        sys.path.insert(0, "tests/consensus/validation")
        from _helpers import _FakeNode, make_block, make_previous_block, seed_storage_account
        from astreum.consensus.transaction.storage.initial import generate_initial_storage_record

        fake = _FakeNode()
        block = make_block(fake, make_previous_block())
        seed_storage_account(block)
        self.store_flat(fake, whole)
        self.cells = dict(fake.hot_storage)
        record, slots, _, _ = generate_initial_storage_record(
            fake, block, whole, follow_refs=True
        )
        return record, slots

    def _run(self, *, record=None, cells=None, registered=(), storage_id=None):
        record = record or self.record
        cells = self.cells if cells is None else cells
        storage_id = storage_id or self.storage_id
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x05" * 32, root_block=None))
        block = SimpleNamespace(accounts=SimpleNamespace(get_account=lambda a, n: account))
        served = []

        def get(n, h, **kw):
            served.append((h, kw.get("block_hash")))
            return cells.get(h)

        with patch(self.GET_BLOCK, return_value=block), \
             patch(self.GET_EXPR, side_effect=get), \
             patch(self.ROOT_GET, side_effect=get), \
             patch(self.TRIE, side_effect=lambda tree, n, h: (1 if h in registered else None)):
            ok = fetch_and_store_record(self.node, storage_id, record)
        self.served = served
        return ok

    def _written(self):
        return get_record_from_cold_storage(self.node, self.storage_id)

    def test_replay_gives_the_creators_slot_list(self):
        self.assertTrue(self._run())
        self.assertEqual(self._written(), b"".join(self.slots))

    def test_the_stored_list_proves_every_slot(self):
        self.assertTrue(self._run())
        blob = self._written()
        ids = [blob[i : i + 32] for i in range(0, len(blob), 32)]
        tree = create_merkle_tree(ids)
        self.assertEqual(tree.root, self.record.slots_root)
        from astreum.crypto.merkle import MerkleTree

        committed = MerkleTree(self.record.slots_root, self.record.new_count)
        for i, slot_id in enumerate(ids):
            self.assertTrue(
                verify_merkle_tree_proof(committed, i, slot_id, generate_merkle_tree_proof(tree, i))
            )

    def test_cells_are_fetched_scoped_to_the_block(self):
        self.assertTrue(self._run())
        fetched = [(h, b) for h, b in self.served if h != self.storage_id]
        self.assertTrue(fetched)
        self.assertEqual({b for _, b in fetched}, {self.storage_id})

    def test_cold_files_written_for_root_and_every_slot(self):
        self.assertTrue(self._run())
        for h in [self.storage_id, *self.slots]:
            self.assertIsNotNone(get_expr_from_cold_storage(self.node, h), h.hex())

    def test_a_missing_cell_writes_no_entry(self):
        cells = dict(self.cells)
        del cells[self.deep.hash()]
        self.assertFalse(self._run(cells=cells))
        self.assertIsNone(self._written())

    def test_a_root_that_does_not_match_is_rejected(self):
        bad = StorageRecord(**{**self.record.__dict__, "slots_root": b"\x77" * 32, "_expr": None})
        self.assertFalse(self._run(record=bad))
        self.assertIsNone(self._written())

    def test_a_count_or_size_that_does_not_match_is_rejected(self):
        for field in ("new_count", "new_size"):
            fields = {**self.record.__dict__, "_expr": None}
            fields[field] += 1
            self.assertFalse(self._run(record=StorageRecord(**fields)), field)
            self.assertIsNone(self._written(), field)

    def test_an_extra_registered_cell_changes_the_list_and_is_rejected(self):
        # The same block replayed against a trie that already holds one of its
        # cells gives another list than the record committed to.
        self.assertFalse(self._run(registered={self.deep.hash()}))
        self.assertIsNone(self._written())

    def test_idempotent(self):
        self.assertTrue(self._run())
        first = self._written()
        self.assertTrue(self._run())
        self.assertEqual(self._written(), first)

    def test_unavailable_block_header_is_a_failure_not_a_raise(self):
        with patch(self.GET_BLOCK, side_effect=ValueError("no header")):
            self.assertFalse(fetch_and_store_record(self.node, self.storage_id, self.record))
        self.assertIsNone(self._written())


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
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x01" * 32, root_block=None))
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
        account = SimpleNamespace(data=SimpleNamespace(root_hash=b"\x01" * 32, root_block=None))
        node = _lt_node(latest_block=_latest_block(account=account))
        record = StorageRecord(new_count=3, slots_root=b"\x04" * 32)
        patcher, fetch = self._patch_fetch(result=True)
        with patcher, patch(
            "astreum.storage.workers.advertisements.get_record_from_cold_storage",
            return_value=None,
        ), patch(
            "astreum.storage.workers.advertisements.get_from_radix_tree",
            return_value=object(),  # record header sentinel
        ), patch(
            "astreum.storage.workers.advertisements.parse_record",
            return_value=record,
        ) as mock_parse:
            self.assertTrue(_long_term_store_one(node))
        mock_parse.assert_called_once()
        fetch.assert_called_once()
        args = fetch.call_args.args
        self.assertEqual(args, (node, REC_HASH, record))
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
