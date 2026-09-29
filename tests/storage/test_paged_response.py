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

from astreum.communication.storage_request.handle import _collect_record_exprs
from astreum.communication.storage_response import handle as resp_handle
from astreum.communication.storage_response.code import StorageResponseCode
from astreum.communication.storage_response.cull import cull_to_resolution
from astreum.communication.storage_response.model import StorageResponse
from astreum.communication.storage_response.storage_found import (
    decode_found_page,
    encode_found_pages,
)
from astreum.expression import (
    NIL,
    RESOLUTION_FULL,
    RESOLUTION_LIST,
    RESOLUTION_RECORD,
    RESOLUTION_SINGLE,
    ZERO32,
    bytes_,
    collect_full,
    collect_list,
    link,
)
from astreum.storage import requests as reqs
from astreum.storage.requests import (
    PendingExprRequest,
    StageResult,
    claim_expr_req,
    has_expr_req,
    stage_found_page,
)

BIG = 30000
H1 = b"\x01" * 32
PEER_A = b"\xa1" * 32
PEER_B = b"\xb2" * 32


def _big(i: int):
    return bytes_(bytes([i]) * BIG)


def _node(ttl: float = 60.0, max_pages=None):
    # Non-reentrant Lock on purpose: registry calls must take it once.
    config = {"expr_request_ttl": ttl}
    if max_pages is not None:
        config["storage_found_max_pages"] = max_pages
    return SimpleNamespace(
        logger=MagicMock(),
        config=config,
        expr_requests={},
        expr_requests_lock=threading.Lock(),
    )


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestStaging(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        patcher = patch.object(reqs.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _claimed(self, ttl=4.0, max_pages=None):
        node = _node(ttl=ttl, max_pages=max_pages)
        claim_expr_req(node, H1, RESOLUTION_FULL)
        return node

    def test_missing_request_ignored(self):
        node = _node()
        self.assertEqual(stage_found_page(node, H1, PEER_A, 1, 1, ["x"])[0], StageResult.IGNORED)

    def test_expired_request_ignored(self):
        node = self._claimed(ttl=4.0)
        self.clock.now += 4
        self.assertEqual(stage_found_page(node, H1, PEER_A, 1, 1, ["x"])[0], StageResult.IGNORED)

    def test_single_page_completes_and_pops(self):
        node = self._claimed()
        result, req = stage_found_page(node, H1, PEER_A, 1, 1, ["x"])
        self.assertEqual(result, StageResult.COMPLETE)
        self.assertIsInstance(req, PendingExprRequest)
        self.assertEqual(req.pages, {1: ["x"]})
        self.assertEqual(req.payload_type, RESOLUTION_FULL)
        self.assertNotIn(H1, node.expr_requests)

    def test_last_page_first_then_rest_any_order(self):
        node = self._claimed()
        self.assertEqual(stage_found_page(node, H1, PEER_A, 3, 3, ["c"])[0], StageResult.STAGED)
        self.assertTrue(has_expr_req(node, H1))
        self.assertEqual(stage_found_page(node, H1, PEER_A, 1, 3, ["a"])[0], StageResult.STAGED)
        result, req = stage_found_page(node, H1, PEER_A, 2, 3, ["b"])
        self.assertEqual(result, StageResult.COMPLETE)
        self.assertEqual(req.pages, {1: ["a"], 2: ["b"], 3: ["c"]})
        self.assertFalse(has_expr_req(node, H1))

    def test_first_accepted_page_binds_sender_and_total(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 2, 3, ["b"])
        req = node.expr_requests[H1]
        self.assertEqual((req.sender, req.total), (PEER_A, 3))

    def test_duplicate_page_ignored(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 1, 2, ["a"])
        self.assertEqual(stage_found_page(node, H1, PEER_A, 1, 2, ["evil"])[0], StageResult.IGNORED)
        self.assertEqual(node.expr_requests[H1].pages, {1: ["a"]})

    def test_other_sender_ignored(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 1, 2, ["a"])
        self.assertEqual(stage_found_page(node, H1, PEER_B, 2, 2, ["b"])[0], StageResult.IGNORED)
        self.assertEqual(set(node.expr_requests[H1].pages), {1})

    def test_different_total_ignored_and_request_still_completes(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 1, 2, ["a"])
        self.assertEqual(stage_found_page(node, H1, PEER_A, 2, 9, ["evil"])[0], StageResult.IGNORED)
        self.assertEqual(node.expr_requests[H1].total, 2)
        result, req = stage_found_page(node, H1, PEER_A, 2, 2, ["b"])
        self.assertEqual(result, StageResult.COMPLETE)
        self.assertEqual(req.pages, {1: ["a"], 2: ["b"]})

    def test_page_above_total_ignored(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 1, 2, ["a"])
        self.assertEqual(stage_found_page(node, H1, PEER_A, 3, 2, ["x"])[0], StageResult.IGNORED)
        self.assertEqual(set(node.expr_requests[H1].pages), {1})

    def test_first_page_above_total_does_not_bind(self):
        node = self._claimed()
        self.assertEqual(stage_found_page(node, H1, PEER_A, 5, 2, ["x"])[0], StageResult.IGNORED)
        self.assertIsNone(node.expr_requests[H1].sender)

    def test_first_page_over_max_pages_ignored_without_binding(self):
        node = self._claimed(max_pages=3)
        deadline = node.expr_requests[H1].expires_at
        self.clock.now += 1
        result, _ = stage_found_page(node, H1, PEER_A, 1, 4, ["x"])
        self.assertEqual(result, StageResult.IGNORED)
        req = node.expr_requests[H1]
        self.assertIsNone(req.sender)
        self.assertIsNone(req.total)
        self.assertEqual(req.pages, {})
        self.assertEqual(req.expires_at, deadline)
        # A later valid page from another peer binds and proceeds.
        self.assertEqual(stage_found_page(node, H1, PEER_B, 1, 3, ["a"])[0], StageResult.STAGED)
        self.assertEqual(node.expr_requests[H1].sender, PEER_B)

    def test_max_pages_is_inclusive(self):
        node = self._claimed(max_pages=3)
        self.assertEqual(stage_found_page(node, H1, PEER_A, 1, 3, ["a"])[0], StageResult.STAGED)

    def test_accepted_pages_slide_expiry_ignored_do_not(self):
        node = self._claimed(ttl=4.0)
        self.clock.now += 3
        stage_found_page(node, H1, PEER_A, 1, 3, ["a"])
        self.assertEqual(node.expr_requests[H1].expires_at, self.clock.now + 4.0)
        self.clock.now += 3
        stage_found_page(node, H1, PEER_A, 1, 3, ["dup"])  # duplicate
        stage_found_page(node, H1, PEER_B, 2, 3, ["other"])  # other sender
        self.assertEqual(node.expr_requests[H1].expires_at, self.clock.now + 1.0)
        stage_found_page(node, H1, PEER_A, 2, 3, ["b"])
        self.assertEqual(node.expr_requests[H1].expires_at, self.clock.now + 4.0)

    def test_expires_ttl_after_last_accepted_page_not_claim(self):
        node = self._claimed(ttl=4.0)
        self.clock.now += 3
        stage_found_page(node, H1, PEER_A, 1, 3, ["a"])
        self.clock.now += 3.9  # 6.9s after claim, 3.9s after last page
        self.assertTrue(has_expr_req(node, H1))
        self.clock.now += 0.2
        self.assertFalse(has_expr_req(node, H1))

    def test_racing_last_page_exactly_one_complete(self):
        node = self._claimed()
        stage_found_page(node, H1, PEER_A, 1, 2, ["a"])
        results = []
        barrier = threading.Barrier(16)

        def worker():
            barrier.wait()
            results.append(stage_found_page(node, H1, PEER_A, 2, 2, ["b"])[0])

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(StageResult.COMPLETE), 1)


def _tree():
    """root -> head subtree (hs) and a tail chain (c2 -> c3 -> NIL)."""
    h1, h2 = bytes_(b"h1"), bytes_(b"h2")
    hs = link(h1, h2)
    c3 = link(bytes_(b"t3"), NIL)
    c2 = link(bytes_(b"t2"), c3)
    root = link(hs, c2)
    return SimpleNamespace(root=root, hs=hs, h1=h1, h2=h2, c2=c2, c3=c3)


def _wire(exprs):
    """Round-trip through the page codec so exprs carry hash references."""
    out = []
    for payload in encode_found_pages(exprs):
        out.extend(decode_found_page(payload)[2])
    return out


def _hashes(exprs):
    return [e.hash() for e in exprs]


def _stored_set(exprs):
    # NIL is referenced as ZERO32 on the wire, so it is never reachable by hash
    # and is not stored.
    return {e.hash() for e in exprs} - {NIL.hash()}


class TestCull(unittest.TestCase):
    def setUp(self):
        self.t = _tree()
        self.wire_full = _wire(collect_full(self.t.root))
        self.root = self.wire_full[0]

    def test_single_keeps_only_root(self):
        kept = cull_to_resolution(self.root, self.wire_full, RESOLUTION_SINGLE)
        self.assertEqual(_hashes(kept), [self.t.root.hash()])

    def test_none_resolution_is_single(self):
        kept = cull_to_resolution(self.root, self.wire_full, None)
        self.assertEqual(_hashes(kept), [self.t.root.hash()])

    def test_list_keeps_tail_chain_only(self):
        kept = cull_to_resolution(self.root, self.wire_full, RESOLUTION_LIST)
        self.assertEqual(
            _hashes(kept), [self.t.root.hash(), self.t.c2.hash(), self.t.c3.hash()]
        )
        self.assertNotIn(self.t.hs.hash(), _hashes(kept))

    def test_list_matches_collect_list_on_real_tree(self):
        expected = {h for h in _hashes(collect_list(self.t.root)) if h != NIL.hash()}
        kept = cull_to_resolution(self.root, self.wire_full, RESOLUTION_LIST)
        self.assertEqual(set(_hashes(kept)), expected)

    def test_full_keeps_reachable_and_drops_junk(self):
        junk = bytes_(b"junk")
        kept = cull_to_resolution(self.root, self.wire_full + [junk], RESOLUTION_FULL)
        got = set(_hashes(kept))
        self.assertNotIn(junk.hash(), got)
        self.assertEqual(got, _stored_set(self.wire_full))

    def test_root_is_first(self):
        shuffled = list(reversed(self.wire_full))
        kept = cull_to_resolution(self.root, shuffled, RESOLUTION_FULL)
        self.assertEqual(kept[0].hash(), self.t.root.hash())

    def test_duplicates_kept_once(self):
        kept = cull_to_resolution(
            self.root, self.wire_full + self.wire_full, RESOLUTION_FULL
        )
        self.assertEqual(len(_hashes(kept)), len(set(_hashes(kept))))
        self.assertEqual(set(_hashes(kept)), _stored_set(self.wire_full))

    def test_cycle_terminates(self):
        a = bytes_(b"a")
        b = bytes_(b"b")
        x = link(a, b)
        # hash references pointing back at the root
        x_cycle = link(None, None)
        x_cycle.head_hash = x.hash()
        x_cycle.tail_hash = x.hash()
        kept = cull_to_resolution(x_cycle, [x_cycle, x, a, b], RESOLUTION_FULL)
        self.assertEqual(len(kept), len({e.hash() for e in kept}))

    def test_omitted_expr_kept_as_delivered(self):
        partial = [e for e in self.wire_full if e.hash() != self.t.h1.hash()]
        kept = cull_to_resolution(self.root, partial, RESOLUTION_FULL)
        self.assertEqual(set(_hashes(kept)), _stored_set(partial))


class TestCullRecord(unittest.TestCase):
    """RECORD reply built through the real serve path (blob + collect)."""

    def setUp(self):
        self.slot_a = link(bytes_(b"slot-a"), NIL)
        self.slot_b = link(bytes_(b"slot-b"), NIL)
        # slot_b sits under `mid`, a node the requester already holds.
        self.mid = link(self.slot_b, NIL)
        self.root = link(self.slot_a, self.mid)
        self.blob = self.slot_a.hash() + ZERO32 + self.slot_b.hash()
        self.junk = bytes_(b"junk")

    def _served(self):
        held = {self.slot_a.hash(): self.slot_a, self.slot_b.hash(): self.slot_b}
        with patch(
            "astreum.communication.storage_request.handle.get_record_from_cold_storage",
            return_value=self.blob,
        ), patch(
            "astreum.communication.storage_request.handle.get_expr_from_local_storage",
            side_effect=lambda _n, h: held.get(h),
        ):
            return _collect_record_exprs(None, self.root, self.root.hash())

    def test_every_nonzero_slot_survives(self):
        wire = _wire(self._served()) + [self.junk]
        root = wire[0]
        local = {self.mid.hash(): self.mid}
        kept = cull_to_resolution(root, wire, RESOLUTION_RECORD, resolve_local=local.get)
        got = set(_hashes(kept))
        for offset in range(0, len(self.blob), 32):
            slot_id = self.blob[offset : offset + 32]
            if slot_id != ZERO32:
                self.assertIn(slot_id, got)
        self.assertNotIn(self.junk.hash(), got)
        self.assertNotIn(self.mid.hash(), got)  # traversed, not re-stored

    def test_unreachable_slot_dropped_without_local_path(self):
        wire = _wire(self._served())
        kept = cull_to_resolution(wire[0], wire, RESOLUTION_RECORD, resolve_local=lambda h: None)
        got = set(_hashes(kept))
        self.assertIn(self.slot_a.hash(), got)
        self.assertNotIn(self.slot_b.hash(), got)


class TestReceiver(unittest.TestCase):
    def _peer(self, key=PEER_A):
        return SimpleNamespace(address=("127.0.0.1", 1), public_key_bytes=key, metrics={})

    def _deliver(self, node, root_id, payload, stored, peer=None, admit=True, admit_calls=None, put=None):
        resp = StorageResponse(StorageResponseCode.STORAGE_FOUND, payload, root_id)
        msg = SimpleNamespace(content=resp.to_bytes())

        def _admit(n, h):
            if admit_calls is not None:
                admit_calls.append(h)
            return admit

        if put is None:
            put = lambda n, e: stored.append(e.hash()) or True
        with patch("astreum.storage.admission.is_expr_in_latest_block", side_effect=_admit), \
             patch.object(resp_handle, "put_expr_in_hot_storage", side_effect=put), \
             patch.object(resp_handle, "get_expr_from_local_storage", return_value=None), \
             patch.object(resp_handle, "increment_peer_metric") as metric:
            self.metric = metric
            return resp_handle.handle_storage_response(node, peer or self._peer(), msg)

    def _paged_full(self):
        exprs = [link(bytes_(b"root"), NIL)] + [_big(i) for i in range(1, 6)]
        # make the big exprs reachable from the root
        node_chain = NIL
        for e in reversed(exprs[1:]):
            node_chain = link(e, node_chain)
        root = node_chain
        exprs = collect_full(root)
        return root, exprs

    def test_last_page_first_stores_nothing_until_complete(self):
        root, exprs = self._paged_full()
        pages = encode_found_pages(exprs)
        self.assertGreater(len(pages), 2)
        node = _node()
        claim_expr_req(node, root.hash(), RESOLUTION_FULL)
        stored = []
        order = [len(pages) - 1] + list(range(len(pages) - 1))
        for n, idx in enumerate(order):
            self.assertEqual(self._deliver(node, root.hash(), pages[idx], stored), (True, None))
            if n < len(order) - 1:
                self.assertEqual(stored, [])
                self.assertTrue(has_expr_req(node, root.hash()))
        self.assertFalse(has_expr_req(node, root.hash()))
        self.assertEqual(set(stored), _stored_set(exprs))
        self.assertEqual(stored[-1], root.hash())  # children before root

    def test_shuffled_delivery_matches_in_order(self):
        root, exprs = self._paged_full()
        pages = encode_found_pages(exprs)

        def run(order):
            node = _node()
            claim_expr_req(node, root.hash(), RESOLUTION_FULL)
            stored = []
            for idx in order:
                self._deliver(node, root.hash(), pages[idx], stored)
            return stored

        in_order = run(range(len(pages)))
        shuffled = run(reversed(range(len(pages))))
        self.assertEqual(set(in_order), set(shuffled))
        self.assertEqual(in_order[-1], shuffled[-1])

    def test_duplicate_and_other_sender_pages_ignored(self):
        root, exprs = self._paged_full()
        pages = encode_found_pages(exprs)
        node = _node()
        claim_expr_req(node, root.hash(), RESOLUTION_FULL)
        stored = []
        self._deliver(node, root.hash(), pages[0], stored)
        self.assertEqual(self._deliver(node, root.hash(), pages[0], stored), (True, None))
        self.assertEqual(
            self._deliver(node, root.hash(), pages[1], stored, peer=self._peer(PEER_B)),
            (True, None),
        )
        self.assertEqual(set(node.expr_requests[root.hash()].pages), {1})
        for idx in range(1, len(pages)):
            self._deliver(node, root.hash(), pages[idx], stored)
        self.assertEqual(set(stored), _stored_set(exprs))

    def test_malformed_page_rejected_request_kept(self):
        node = _node()
        rid = b"\x05" * 32
        claim_expr_req(node, rid, RESOLUTION_FULL)
        ok, reason = self._deliver(node, rid, b"\x01\x00\x00\x00\x01\x00\x01", [])
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid STORAGE_FOUND payload")
        self.assertTrue(has_expr_req(node, rid))

    def test_unknown_request_dropped_at_gate(self):
        stored = []
        payload = encode_found_pages([bytes_(b"x")])[0]
        self.assertEqual(self._deliver(_node(), b"\x07" * 32, payload, stored), (True, None))
        self.assertEqual(stored, [])

    def test_single_request_answered_with_full_tree_stores_root_only(self):
        t = _tree()
        exprs = collect_full(t.root)
        node = _node()
        claim_expr_req(node, t.root.hash(), RESOLUTION_SINGLE)
        stored = []
        for payload in encode_found_pages(exprs):
            self._deliver(node, t.root.hash(), payload, stored)
        self.assertEqual(stored, [t.root.hash()])

    def test_list_request_answered_with_full_tree_drops_head_subtree(self):
        t = _tree()
        node = _node()
        claim_expr_req(node, t.root.hash(), RESOLUTION_LIST)
        stored = []
        for payload in encode_found_pages(collect_full(t.root)):
            self._deliver(node, t.root.hash(), payload, stored)
        self.assertEqual(set(stored), {t.root.hash(), t.c2.hash(), t.c3.hash()})
        self.assertEqual(stored[-1], t.root.hash())

    def test_full_request_drops_injected_junk(self):
        t = _tree()
        junk = bytes_(b"junk")
        node = _node()
        claim_expr_req(node, t.root.hash(), RESOLUTION_FULL)
        stored = []
        for payload in encode_found_pages(collect_full(t.root) + [junk]):
            self._deliver(node, t.root.hash(), payload, stored)
        self.assertNotIn(junk.hash(), stored)
        self.assertIn(t.hs.hash(), stored)

    def test_uncommitted_root_stores_nothing_and_pops_once(self):
        root, exprs = self._paged_full()
        pages = encode_found_pages(exprs)
        node = _node()
        claim_expr_req(node, root.hash(), RESOLUTION_FULL)
        stored, calls = [], []
        results = [
            self._deliver(node, root.hash(), p, stored, admit=False, admit_calls=calls)
            for p in pages
        ]
        self.assertEqual(results[:-1], [(True, None)] * (len(pages) - 1))
        self.assertEqual(results[-1], (False, "uncommitted data rejected"))
        self.assertEqual(stored, [])
        self.assertFalse(has_expr_req(node, root.hash()))
        self.assertEqual(len(calls), 1)  # once per reply, not per page

    def test_no_expr_hashing_to_expr_id_stores_nothing(self):
        node = _node()
        rid = b"\x09" * 32
        claim_expr_req(node, rid, RESOLUTION_FULL)
        stored = []
        payload = encode_found_pages([bytes_(b"not-the-root")])[0]
        ok, reason = self._deliver(node, rid, payload, stored)
        self.assertEqual((ok, reason), (False, "STORAGE_FOUND root ID mismatch"))
        self.assertEqual(stored, [])
        self.assertFalse(has_expr_req(node, rid))

    def test_hot_storage_failures_reported(self):
        t = _tree()
        node = _node()
        claim_expr_req(node, t.root.hash(), RESOLUTION_FULL)
        ok, reason = False, None
        for payload in encode_found_pages(collect_full(t.root)):
            ok, reason = self._deliver(
                node, t.root.hash(), payload, [], put=lambda n, e: False
            )
        self.assertFalse(ok)
        self.assertIn(f"count={len(_stored_set(collect_full(t.root)))}", reason)

    def test_download_metric_counts_kept_bytes_only(self):
        from astreum.expression.encoding import encode_expr_to_bytes

        t = _tree()
        node = _node()
        claim_expr_req(node, t.root.hash(), RESOLUTION_SINGLE)
        for payload in encode_found_pages(collect_full(t.root)):
            self._deliver(node, t.root.hash(), payload, [])
        args = self.metric.call_args[0]
        self.assertEqual(args[1], "shared_storage_download")
        self.assertEqual(args[2], len(encode_expr_to_bytes(t.root)))

    def test_first_page_over_max_pages_ignored_by_receiver(self):
        root, exprs = self._paged_full()
        pages = encode_found_pages(exprs)
        node = _node(max_pages=1)
        claim_expr_req(node, root.hash(), RESOLUTION_FULL)
        stored = []
        self.assertEqual(self._deliver(node, root.hash(), pages[0], stored), (True, None))
        self.assertIsNone(node.expr_requests[root.hash()].sender)
        self.assertEqual(stored, [])


if __name__ == "__main__":
    unittest.main()
