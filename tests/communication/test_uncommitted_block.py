import queue
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, "src")

from astreum.communication.handlers import ping as ping_handler
from astreum.communication.models.ping import Ping
from astreum.communication.storage_request import handle as req_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_found import decode_found_page
from astreum.expression import RESOLUTION_RECORD, RESOLUTION_SINGLE, bytes_, link, NIL


def _block_and_inner():
    inner = [bytes_(b"inner-%d" % i) for i in range(3)]
    block = link(bytes_(b"block"), NIL)
    return block, inner


class TestServeUncommittedBlock(unittest.TestCase):
    def setUp(self):
        self.block, self.inner = _block_and_inner()
        self.committed = bytes_(b"committed")
        self.local = {
            self.block.hash(): self.block,
            self.committed.hash(): self.committed,
        }

    def _node(self, uncommitted):
        return SimpleNamespace(
            logger=MagicMock(),
            storage_public_key_bytes=b"\x00" * 32,
            storage_index={},
            storage_providers=[],
            outgoing_queue=queue.Queue(),
            config={},
            uncommitted_block=uncommitted,
            peer_route=SimpleNamespace(closest_peer_for_hash=lambda h: None),
        )

    def _serve(self, node, expr_id, resolution=RESOLUTION_RECORD, record_blob=None):
        request = StorageRequest(
            StorageRequestCode.STORAGE_GET, entries=[(expr_id, resolution)]
        )
        message = SimpleNamespace(content=request.to_bytes())
        sent = []
        peer = SimpleNamespace(
            address=("127.0.0.1", 1), shared_key_bytes=b"\x01" * 32, difficulty=1, metrics={}
        )
        with patch.object(
            req_handle, "get_expr_from_local_storage", side_effect=lambda n, h: self.local.get(h)
        ), patch.object(
            req_handle, "_requires_storage_channel", return_value=False
        ), patch.object(
            req_handle, "get_record_from_cold_storage", return_value=record_blob
        ), patch.object(
            req_handle,
            "enqueue_outgoing",
            side_effect=lambda n, a, message, difficulty=1: sent.append(message) or True,
        ), patch.object(req_handle, "increment_peer_metric"):
            result = req_handle.handle_storage_request(node, peer, message)
        responses = [StorageResponse.from_bytes(m.content) for m in sent]
        return result, responses

    def test_serves_block_then_new_exprs(self):
        node = self._node((self.block.hash(), [self.block, *self.inner]))
        result, responses = self._serve(node, self.block.hash())
        self.assertEqual(result, (True, None))
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].code, StorageResponseCode.STORAGE_FOUND)
        self.assertEqual(responses[0].expr_id, self.block.hash())
        _, _, exprs = decode_found_page(responses[0].data)
        self.assertEqual(
            [e.hash() for e in exprs], [e.hash() for e in [self.block, *self.inner]]
        )

    def test_older_block_uses_records_table_once_replaced(self):
        newer = bytes_(b"newer-block")
        node = self._node((newer.hash(), [newer]))
        # block is no longer the uncommitted one and has no record
        result, responses = self._serve(node, self.block.hash(), record_blob=None)
        self.assertEqual(result, (False, "not a record"))
        self.assertEqual(responses, [])

    def test_other_hash_unchanged(self):
        node = self._node((self.block.hash(), [self.block, *self.inner]))
        result, responses = self._serve(node, self.committed.hash(), RESOLUTION_SINGLE)
        self.assertEqual(result, (True, None))
        _, _, exprs = decode_found_page(responses[0].data)
        self.assertEqual([e.hash() for e in exprs], [self.committed.hash()])

    def test_no_uncommitted_block_unchanged(self):
        node = self._node(None)
        result, responses = self._serve(node, self.block.hash(), record_blob=None)
        self.assertEqual(result, (False, "not a record"))


class TestPingRequestsUncommittedBlock(unittest.TestCase):
    BLOCK = b"\x07" * 32

    def _node(self):
        return SimpleNamespace(
            logger=MagicMock(),
            latest_block_lock=threading.RLock(),
            latest_block_hash=None,
            latest_block=None,
            validation_route=None,
        )

    def _peer(self):
        return SimpleNamespace(
            timestamp=None,
            latest_block=None,
            difficulty=1,
            is_default_seed=False,
            address=("10.0.0.5", 5000),
            relay_public_key_bytes=b"\x05" * 32,
            public_key_bytes=b"\x06" * 32,
        )

    def _ping(self, node, peer, *, is_validator=True, latest=BLOCK, local=None):
        payload = Ping(is_validator=is_validator, difficulty=1, latest_block=latest).to_bytes()
        with patch("astreum.storage.exprs.get_expr_from_local_storage", return_value=local), \
             patch("astreum.storage.workers.requests.queue_storage_get") as queue_get:
            ping_handler.handle_ping(node, peer, payload)
        return queue_get

    def test_validator_ping_for_unknown_head_queues_record_get(self):
        node, peer = self._node(), self._peer()
        queue_get = self._ping(node, peer)
        queue_get.assert_called_once_with(
            node,
            self.BLOCK,
            RESOLUTION_RECORD,
            contact=(b"\x05" * 32, "10.0.0.5", 5000),
        )

    def test_local_head_queues_nothing(self):
        queue_get = self._ping(self._node(), self._peer(), local=bytes_(b"have it"))
        queue_get.assert_not_called()

    def test_non_validator_ping_queues_nothing(self):
        queue_get = self._ping(self._node(), self._peer(), is_validator=False)
        queue_get.assert_not_called()

    def test_ping_without_head_queues_nothing(self):
        queue_get = self._ping(self._node(), self._peer(), latest=None)
        queue_get.assert_not_called()


class TestWorkerNoBlockAds(unittest.TestCase):
    def test_worker_does_not_advertise(self):
        from astreum.consensus.validation import worker

        self.assertFalse(hasattr(worker, "advertise_exprs"))
        self.assertFalse(hasattr(worker, "validator_advertisment_limit_seconds"))


if __name__ == "__main__":
    unittest.main()
