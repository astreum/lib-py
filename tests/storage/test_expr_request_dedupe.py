"""Tests for the pending-expr-request registry: expiry, dedupe, sweep, ttl config."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from astreum.expression import RESOLUTION_SINGLE, RESOLUTION_RECORD
from astreum.storage import requests as reqs
from astreum.storage.requests import (
    PendingExprRequest,
    claim_expr_req,
    get_expr_req_payload,
    has_expr_req,
    pop_expr_req,
    prune_expired_expr_reqs,
)
from astreum.utils.config import config_setup

H1 = b"\x01" * 32
H2 = b"\x02" * 32


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _node(ttl: float = 4.0):
    # Non-reentrant Lock on purpose: registry calls must take it once.
    return SimpleNamespace(
        config={"expr_request_ttl": ttl},
        expr_requests={},
        expr_requests_lock=threading.Lock(),
    )


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        patcher = patch.object(reqs.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claim_then_skip_at_any_resolution(self):
        node = _node()
        self.assertTrue(claim_expr_req(node, H1, RESOLUTION_SINGLE))
        self.assertFalse(claim_expr_req(node, H1, RESOLUTION_SINGLE))
        self.assertFalse(claim_expr_req(node, H1, RESOLUTION_RECORD))
        self.assertEqual(get_expr_req_payload(node, H1), RESOLUTION_SINGLE)

    def test_record_pending_blocks_single(self):
        node = _node()
        self.assertTrue(claim_expr_req(node, H1, RESOLUTION_RECORD))
        self.assertFalse(claim_expr_req(node, H1, RESOLUTION_SINGLE))
        self.assertEqual(get_expr_req_payload(node, H1), RESOLUTION_RECORD)

    def test_expired_entry_is_dead_without_sweep(self):
        node = _node(ttl=4.0)
        claim_expr_req(node, H1, RESOLUTION_SINGLE)
        self.clock.now += 4.0
        self.assertFalse(has_expr_req(node, H1))
        self.assertIsNone(get_expr_req_payload(node, H1))
        self.assertIn(H1, node.expr_requests)  # still in dict, not swept
        self.assertTrue(claim_expr_req(node, H1, RESOLUTION_SINGLE))

    def test_pop_returns_payload_and_none_when_expired(self):
        node = _node()
        claim_expr_req(node, H1, RESOLUTION_RECORD)
        self.assertEqual(pop_expr_req(node, H1), RESOLUTION_RECORD)
        self.assertFalse(has_expr_req(node, H1))
        claim_expr_req(node, H2, RESOLUTION_SINGLE)
        self.clock.now += 10
        self.assertIsNone(pop_expr_req(node, H2))
        self.assertNotIn(H2, node.expr_requests)

    def test_request_functions_do_not_scan(self):
        node = _node()
        claim_expr_req(node, H1, RESOLUTION_SINGLE)
        self.clock.now += 10
        claim_expr_req(node, H2, RESOLUTION_SINGLE)
        has_expr_req(node, H2)
        self.assertIn(H1, node.expr_requests)

    def test_prune_removes_only_expired(self):
        node = _node(ttl=4.0)
        claim_expr_req(node, H1, RESOLUTION_SINGLE)
        self.clock.now += 3
        claim_expr_req(node, H2, RESOLUTION_SINGLE)
        self.clock.now += 1.5  # H1 expired, H2 live
        self.assertEqual(prune_expired_expr_reqs(node), 1)
        self.assertNotIn(H1, node.expr_requests)
        self.assertIn(H2, node.expr_requests)
        self.assertEqual(prune_expired_expr_reqs(node), 0)

    def test_prune_frees_staged_pages_of_idle_entry(self):
        node = _node(ttl=4.0)
        claim_expr_req(node, H1, RESOLUTION_SINGLE)
        node.expr_requests[H1].pages[1] = ["staged"]
        self.clock.now += 5
        self.assertEqual(prune_expired_expr_reqs(node), 1)
        self.assertNotIn(H1, node.expr_requests)

    def test_claim_creates_pending_request_object(self):
        node = _node(ttl=4.0)
        claim_expr_req(node, H1, RESOLUTION_RECORD)
        req = node.expr_requests[H1]
        self.assertIsInstance(req, PendingExprRequest)
        self.assertEqual(req.payload_type, RESOLUTION_RECORD)
        self.assertEqual(req.expires_at, self.clock.now + 4.0)
        self.assertIsNone(req.sender)
        self.assertIsNone(req.total)
        self.assertEqual(req.pages, {})

    def test_racing_claims_exactly_one_wins(self):
        node = _node()
        results = []
        barrier = threading.Barrier(16)

        def worker():
            barrier.wait()
            results.append(claim_expr_req(node, H1, RESOLUTION_SINGLE))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1)


class TestTtlConfig(unittest.TestCase):
    def test_default_is_twice_poll_window(self):
        cfg = config_setup({"storage_fetch_retries": 8, "storage_fetch_interval": 0.25})
        self.assertEqual(cfg["expr_request_ttl"], 4.0)

    def test_default_derives_from_overrides(self):
        cfg = config_setup({"storage_fetch_retries": 5, "storage_fetch_interval": 1})
        self.assertEqual(cfg["expr_request_ttl"], 10.0)

    def test_override_honoured(self):
        cfg = config_setup({"expr_request_ttl": 7})
        self.assertEqual(cfg["expr_request_ttl"], 7.0)

    def test_invalid_values_rejected(self):
        for bad in (0, -1, "abc", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"expr_request_ttl": bad})


class TestFoundMaxPagesConfig(unittest.TestCase):
    def test_default_is_64(self):
        self.assertEqual(config_setup({})["storage_found_max_pages"], 64)

    def test_override_honoured(self):
        self.assertEqual(config_setup({"storage_found_max_pages": 5})["storage_found_max_pages"], 5)

    def test_invalid_values_rejected(self):
        for bad in (0, -1, "abc", True, 1.5, None, 65536):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config_setup({"storage_found_max_pages": bad})


class TestSweepWorker(unittest.TestCase):
    def test_prune_runs_with_pricing_disabled(self):
        from astreum.storage.workers import advertisements as adv

        stop = threading.Event()
        node = SimpleNamespace(
            config={"storage_request_price_interval": 0, "expr_request_ttl": 0.05},
            logger=MagicMock(),
            communication_stop_event=stop,
            long_term_storage=False,
            long_term_storage_interval=0,
            expr_requests={H1: PendingExprRequest(RESOLUTION_SINGLE, 0.0)},  # long expired
            expr_requests_lock=threading.Lock(),
        )
        t = threading.Thread(target=adv.advertise_storage, args=(node,), daemon=True)
        t.start()
        try:
            for _ in range(100):
                if not node.expr_requests:
                    break
                stop.wait(0.05)
            self.assertEqual(node.expr_requests, {})
        finally:
            stop.set()
            t.join(timeout=2)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
