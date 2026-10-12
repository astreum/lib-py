import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.communication.storage_request.model import max_batch_entries
from astreum.expression import RESOLUTION_SINGLE
from astreum.storage.workers.advertisements import (
    _startup_announce_step,
    advertise_storage,
)
from astreum.utils.config import DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES

PUT = "astreum.storage.workers.advertisements.put_exprs_in_network"
ITER = "astreum.storage.workers.advertisements.iter_records_in_cold_storage"


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


def _node(provider=True, synced=True, budget=DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES):
    return SimpleNamespace(
        is_storage_provider=provider,
        latest_block=object() if synced else None,
        startup_announce=None,
        config={"storage_put_batch_max_bytes": budget},
        logger=_silent_logger(),
    )


def _ids(count):
    return [i.to_bytes(32, "big") for i in range(1, count + 1)]


def _announced(put_mock):
    ids = []
    for call in put_mock.call_args_list:
        ids.extend(entry[0] for entry in call.args[1])
    return ids


class TestStartupAnnounceStep(unittest.TestCase):
    def test_skips_when_not_provider(self):
        node = _node(provider=False)
        with patch(PUT) as put, patch(ITER, return_value=iter(_ids(3))):
            self.assertEqual(_startup_announce_step(node), "done")
        put.assert_not_called()

    def test_waits_for_latest_block(self):
        node = _node(synced=False)
        with patch(PUT) as put, patch(ITER, return_value=iter(_ids(3))) as it:
            self.assertEqual(_startup_announce_step(node), "wait")
        put.assert_not_called()
        it.assert_not_called()
        self.assertIsNone(node.startup_announce)

    def test_announces_all_records(self):
        node = _node()
        records = _ids(5)
        with patch(PUT) as put, patch(ITER, return_value=iter(records)):
            self.assertEqual(_startup_announce_step(node), "done")
        self.assertEqual(_announced(put), records)
        for call in put.call_args_list:
            self.assertTrue(all(e[1] == RESOLUTION_SINGLE for e in call.args[1]))

    def test_batch_size_from_budget(self):
        node = _node()
        per_batch = max_batch_entries(DEFAULT_STORAGE_PUT_BATCH_MAX_BYTES)
        records = _ids(per_batch + 2)
        with patch(PUT) as put, patch(ITER, return_value=iter(records)):
            self.assertEqual(_startup_announce_step(node), "more")
            self.assertEqual(len(put.call_args.args[1]), per_batch)
            self.assertEqual(_startup_announce_step(node), "done")
            self.assertEqual(len(put.call_args.args[1]), 2)
        self.assertEqual(_announced(put), records)

    def test_runs_once(self):
        node = _node()
        with patch(PUT) as put, patch(ITER, return_value=iter(_ids(2))):
            self.assertEqual(_startup_announce_step(node), "done")
            self.assertEqual(_startup_announce_step(node), "done")
        self.assertEqual(put.call_count, 1)

    def test_no_records_no_send(self):
        node = _node()
        with patch(PUT) as put, patch(ITER, return_value=iter([])):
            self.assertEqual(_startup_announce_step(node), "done")
        put.assert_not_called()
        self.assertEqual(node.startup_announce, "done")

    def test_error_ends_job(self):
        node = _node()
        with patch(PUT, side_effect=RuntimeError("boom")) as put, patch(
            ITER, return_value=iter(_ids(3))
        ):
            self.assertEqual(_startup_announce_step(node), "done")
            self.assertEqual(_startup_announce_step(node), "done")
        self.assertEqual(put.call_count, 1)


class TestStartupAnnounceLoop(unittest.TestCase):
    def _run(self, node, duration):
        thread = threading.Thread(target=advertise_storage, args=(node,), daemon=True)
        thread.start()
        time.sleep(duration)
        node.communication_stop_event.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def _loop_node(self, **kwargs):
        node = _node(**kwargs)
        node.config["storage_request_price_interval"] = 0
        node.config["expr_request_ttl"] = 0
        node.long_term_storage_interval = 0
        node.peers = {}
        node.peers_lock = threading.RLock()
        node.index_requests = []
        node.index_requests_lock = threading.Lock()
        node.index_requested = False
        node.communication_stop_event = threading.Event()
        return node

    def test_loop_announces_when_synced(self):
        node = self._loop_node()
        records = _ids(3)
        with patch(PUT) as put, patch(ITER, return_value=iter(records)):
            self._run(node, 0.3)
        self.assertEqual(_announced(put), records)

    def test_loop_does_not_announce_when_not_provider(self):
        node = self._loop_node(provider=False)
        with patch(PUT) as put, patch(ITER, return_value=iter(_ids(3))):
            self._run(node, 0.3)
        put.assert_not_called()

    def test_loop_survives_error(self):
        node = self._loop_node()
        with patch(PUT, side_effect=RuntimeError("boom")), patch(
            ITER, return_value=iter(_ids(3))
        ):
            self._run(node, 0.3)
        self.assertEqual(node.startup_announce, "done")


if __name__ == "__main__":
    unittest.main()
