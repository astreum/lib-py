import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, "src")

from astreum.communication.storage_request import handle as put_handle
from astreum.communication.storage_request.code import StorageRequestCode
from astreum.communication.storage_request.model import StorageRequest
from astreum.storage.workers.advertisements import _serve_one_put, advertise_storage
from astreum.utils.config import NodeConfig

HANDLE_PUT = "astreum.communication.storage_request.handle.handle_put"
PROVIDER = b"P" * 70


def _silent_logger():
    return SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )


def _node():
    return SimpleNamespace(
        logger=_silent_logger(),
        put_requests=[],
        put_requests_lock=threading.Lock(),
        index_requests=[],
        index_requests_lock=threading.Lock(),
        index_requested=False,
        is_storage_provider=False,
        latest_block=None,
        config=NodeConfig(
            storage_request_price_interval=0,
            expr_request_ttl=0,
            self_lookup_rounds=0,
        ),
        long_term_storage_interval=0,
        peers={},
        peers_lock=threading.RLock(),
        communication_stop_event=threading.Event(),
    )


def _peer(port=9001):
    return SimpleNamespace(address=("127.0.0.1", port), public_key_bytes=b"\x55" * 32)


def _request(prefix=1):
    entry = (bytes([prefix]) + b"\x00" * 31, 4)
    return StorageRequest(StorageRequestCode.STORAGE_PUT, PROVIDER, entries=[entry])


def _message(request):
    return SimpleNamespace(content=request.to_bytes())


class TestHandlerQueues(unittest.TestCase):
    def test_handler_queues_a_put_and_returns_ok(self):
        node = _node()
        request = _request()
        peer = _peer()
        with patch("astreum.storage.admission.is_expr_in_latest_block") as admit:
            result = put_handle.handle_storage_request(node, peer, _message(request))
        self.assertEqual(result, (True, None))
        admit.assert_not_called()
        self.assertEqual(len(node.put_requests), 1)
        queued_peer, queued_request = node.put_requests[0]
        self.assertIs(queued_peer, peer)
        self.assertEqual(queued_request.entries, request.entries)

    def test_handler_sends_no_network_request(self):
        node = _node()
        with patch("astreum.storage.workers.requests.queue_storage_get") as get, patch(
            "astreum.storage.admission.is_expr_in_latest_block"
        ):
            put_handle.handle_storage_request(node, _peer(), _message(_request()))
        get.assert_not_called()

    def test_handler_does_not_wait_on_a_blocked_admission_check(self):
        node = _node()
        release = threading.Event()

        def blocked(*_args, **_kwargs):
            release.wait(5.0)
            return True

        with patch("astreum.storage.admission.is_expr_in_latest_block", side_effect=blocked):
            started = time.monotonic()
            put_handle.handle_storage_request(node, _peer(), _message(_request()))
            elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(node.put_requests), 1)

    def test_handler_keeps_puts_in_arrival_order(self):
        node = _node()
        for prefix in (1, 2, 3):
            put_handle.handle_storage_request(node, _peer(), _message(_request(prefix)))
        order = [req.entries[0][0][0] for _, req in node.put_requests]
        self.assertEqual(order, [1, 2, 3])


class TestServeOnePut(unittest.TestCase):
    def test_runs_handle_put_with_the_queued_peer_and_request(self):
        node = _node()
        peer, request = _peer(), _request()
        node.put_requests.append((peer, request))
        with patch(HANDLE_PUT, return_value=(True, None)) as handle:
            self.assertFalse(_serve_one_put(node))
        handle.assert_called_once_with(node, peer, request)
        self.assertEqual(node.put_requests, [])

    def test_returns_true_while_items_remain(self):
        node = _node()
        node.put_requests.extend([(_peer(), _request(1)), (_peer(), _request(2))])
        with patch(HANDLE_PUT, return_value=(True, None)) as handle:
            self.assertTrue(_serve_one_put(node))
            self.assertFalse(_serve_one_put(node))
        self.assertEqual(handle.call_count, 2)

    def test_empty_queue_returns_false_and_calls_nothing(self):
        node = _node()
        with patch(HANDLE_PUT) as handle:
            self.assertFalse(_serve_one_put(node))
        handle.assert_not_called()

    def test_logs_a_rejection_and_returns_normally(self):
        node = _node()
        messages = []
        node.logger.debug = lambda *args, **kwargs: messages.append(args)
        node.put_requests.append((_peer(), _request()))
        with patch(HANDLE_PUT, return_value=(False, "STORAGE_PUT rejected: no exprs committed")):
            self.assertFalse(_serve_one_put(node))
        self.assertTrue(any("no exprs committed" in str(m) for m in messages))


class TestLoop(unittest.TestCase):
    def _run(self, node, duration):
        thread = threading.Thread(target=advertise_storage, args=(node,), daemon=True)
        thread.start()
        time.sleep(duration)
        node.communication_stop_event.set()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_loop_serves_a_queued_put(self):
        node = _node()
        node.put_requests.append((_peer(), _request()))
        with patch(HANDLE_PUT, return_value=(True, None)) as handle:
            self._run(node, 0.3)
        self.assertEqual(handle.call_count, 1)
        self.assertEqual(node.put_requests, [])

    def test_loop_drains_several_puts_without_the_idle_wait(self):
        node = _node()
        node.put_requests.extend((_peer(), _request(i)) for i in range(1, 5))
        with patch(HANDLE_PUT, return_value=(True, None)) as handle:
            self._run(node, 0.3)
        self.assertEqual(handle.call_count, 4)

    def test_a_failing_put_does_not_stop_the_loop(self):
        node = _node()
        node.put_requests.extend([(_peer(), _request(1)), (_peer(), _request(2))])
        calls = []

        def handle(_node, _peer, request):
            calls.append(request.entries[0][0][0])
            if request.entries[0][0][0] == 1:
                raise RuntimeError("boom")
            return True, None

        with patch(HANDLE_PUT, side_effect=handle):
            self._run(node, 2.5)
        self.assertEqual(calls, [1, 2])

    def test_put_wait_does_not_block_a_reply_on_another_thread(self):
        """A slow put runs on the loop thread, not on the thread that holds replies."""
        node = _node()
        node.put_requests.append((_peer(), _request()))
        seen = {}

        def slow(*_args, **_kwargs):
            seen["thread"] = threading.current_thread()
            time.sleep(0.4)
            return True, None

        caller = threading.current_thread()
        with patch(HANDLE_PUT, side_effect=slow):
            self._run(node, 0.8)
        self.assertIsNot(seen["thread"], caller)


if __name__ == "__main__":
    unittest.main()
