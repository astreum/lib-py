"""Tests for the second provider try in get_exprs_from_network."""
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import astreum.storage.exprs.network as net
from astreum.communication.models.relay_endpoint import RelayEndpoint
from astreum.expression import RESOLUTION_FULL, RESOLUTION_SINGLE, bytes_
from astreum.storage.requests import (
    claim_expr_req,
    has_expr_req,
    pop_expr_req,
    rearm_expr_req,
    set_next_contact,
    take_next_contact,
)

H = b"\x11" * 32
CONTACT = RelayEndpoint(b"\x21" * 32, ("10.0.0.9", 7000))
RELAY_B = b"\x32" * 32
PAYLOAD_B = b"\x31" * 32 + RELAY_B + socket.inet_aton("10.0.0.8") + (6000).to_bytes(2, "big")
PAYLOAD_A = b"\x41" * 32 + b"\x42" * 32 + socket.inet_aton("10.0.0.7") + (5000).to_bytes(2, "big")
QUEUE = "astreum.storage.workers.requests.queue_storage_get"


def _node():
    return SimpleNamespace(
        is_connected=True,
        config={"storage_fetch_interval": 0.0, "storage_fetch_retries": 1},
        logger=MagicMock(),
        storage_index={},
        storage_providers=[PAYLOAD_A, PAYLOAD_B],
        expr_requests={},
        expr_requests_lock=threading.RLock(),
    )


class _Run:
    """Run get_exprs_from_network with the first poll missing."""

    def __init__(self, node, polls, **kwargs):
        self.node = node
        self.queued = []
        self.polls = list(polls)
        self.kwargs = kwargs

    def __call__(self, entries=((H, RESOLUTION_SINGLE),)):
        def fake_queue(node, expr_id, resolution, **kw):
            self.queued.append((expr_id, kw))
            return True

        def fake_poll(node, ids):
            return {h: self.polls.pop(0) for h in ids}

        with patch(QUEUE, side_effect=fake_queue), \
             patch.object(net, "get_expr_from_local_storage", return_value=None), \
             patch.object(net, "_poll_local", side_effect=fake_poll):
            return net.get_exprs_from_network(self.node, list(entries), **self.kwargs)


class TestSecondTry(unittest.TestCase):
    def test_first_provider_answers_no_resend(self):
        run = _Run(_node(), [bytes_(b"x")])
        run()
        self.assertEqual(len(run.queued), 1)

    def test_resend_to_index_second_provider(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        run = _Run(node, [None, bytes_(b"x")])
        result = run()
        self.assertEqual(len(run.queued), 2)
        queued = run.queued[1][1]["contact"]
        self.assertEqual((queued.relay_key, queued.address), (RELAY_B, ("10.0.0.8", 6000)))
        self.assertIsNotNone(result[H])

    def test_resend_to_pending_next_contact(self):
        node = _node()
        claim_expr_req(node, H, RESOLUTION_SINGLE)
        set_next_contact(node, H, CONTACT)
        run = _Run(node, [None, None])
        run()
        self.assertEqual(run.queued[1][1]["contact"], CONTACT)
        self.assertTrue(run.queued[1][1]["preclaimed"])

    def test_resend_rearms_deadline(self):
        node = _node()
        claim_expr_req(node, H, RESOLUTION_SINGLE)
        node.expr_requests[H].expires_at -= 3.0
        before = node.expr_requests[H].expires_at
        set_next_contact(node, H, CONTACT)
        _Run(node, [None, None])()
        self.assertGreater(node.expr_requests[H].expires_at, before + 3.0)

    def test_resend_after_request_expired(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        run = _Run(node, [None, None])
        run()
        self.assertFalse(run.queued[1][1]["preclaimed"])

    def test_no_second_provider_returns_none(self):
        node = _node()
        node.storage_index[H] = (0,)
        run = _Run(node, [None])
        result = run()
        self.assertEqual(len(run.queued), 1)
        self.assertIsNone(result[H])

    def test_only_one_resend(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        run = _Run(node, [None, None])
        result = run()
        self.assertEqual(len(run.queued), 2)
        self.assertIsNone(result[H])

    def test_explicit_contact_gets_no_second_try(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        run = _Run(node, [None], contact=CONTACT)
        run()
        self.assertEqual(len(run.queued), 1)

    def test_structured_resend_only_when_root_missing(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        resolved = []
        run = _Run(node, [])

        def fake_resolve(n, expr_id, resolution, contact=None, block_hash=None):
            resolved.append(expr_id)
            return bytes_(b"root")

        with patch.object(net, "_resolve_structured", side_effect=fake_resolve):
            run(entries=((H, RESOLUTION_FULL),))
        self.assertEqual(len(run.queued), 1)
        self.assertEqual(resolved, [H])

    def test_structured_resend_when_root_missing(self):
        node = _node()
        node.storage_index[H] = (0, 1)
        results = [None, bytes_(b"root")]
        run = _Run(node, [])
        with patch.object(net, "_resolve_structured", side_effect=lambda *a, **k: results.pop(0)):
            result = run(entries=((H, RESOLUTION_FULL),))
        self.assertEqual(len(run.queued), 2)
        self.assertIsNotNone(result[H])


class TestNextContact(unittest.TestCase):
    def test_next_contact_dropped_with_request(self):
        node = _node()
        claim_expr_req(node, H, RESOLUTION_SINGLE)
        set_next_contact(node, H, CONTACT)
        pop_expr_req(node, H)
        self.assertIsNone(take_next_contact(node, H))

    def test_take_next_contact_clears(self):
        node = _node()
        claim_expr_req(node, H, RESOLUTION_SINGLE)
        set_next_contact(node, H, CONTACT)
        self.assertEqual(take_next_contact(node, H), CONTACT)
        self.assertIsNone(take_next_contact(node, H))

    def test_set_without_live_request(self):
        node = _node()
        self.assertFalse(set_next_contact(node, H, CONTACT))
        self.assertFalse(rearm_expr_req(node, H))
        self.assertFalse(has_expr_req(node, H))


if __name__ == "__main__":
    unittest.main()
