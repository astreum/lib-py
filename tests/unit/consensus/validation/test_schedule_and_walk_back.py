"""The validation worker draws by time and walks back from a head it cannot load."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.block.encoding.decode import get_previous_block_hash_local  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.consensus.account.model import ACCOUNT_SHAPE  # noqa: E402
from astreum.consensus.block.nonce import generate_block_nonce  # noqa: E402
from astreum.consensus.transaction.storage.model import STORAGE_RECORD_SHAPE  # noqa: E402
from astreum.consensus.validation import worker as worker_module  # noqa: E402
from astreum.consensus.validation.genesis import create_genesis_block  # noqa: E402
from astreum.consensus.validation.worker import (  # noqa: E402
    _walk_back,
    make_validation_worker,
)
from astreum.expression import ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.exprs import put_expr_in_hot_storage  # noqa: E402


def _h(n: int) -> bytes:
    return bytes([n]) * 32


def _node(head=None, latest_block=None):
    return SimpleNamespace(
        logger=mock.Mock(),
        latest_block_hash=head,
        latest_block=latest_block,
        latest_block_lock=threading.Lock(),
        block_spacing=2,
        nonce_time_ms=0,
    )


class TestWalkBack(unittest.TestCase):
    def test_uses_the_parent_when_the_header_is_local(self):
        node = _node(head=_h(2))
        with mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=_h(1)):
            self.assertEqual(_walk_back(node, _h(2), _h(9)), _h(1))
        self.assertEqual(node.latest_block_hash, _h(1))
        self.assertIsNone(node.latest_block)

    def test_uses_last_good_head_without_a_header(self):
        node = _node(head=_h(2))
        with mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=None):
            self.assertEqual(_walk_back(node, _h(2), _h(9)), _h(9))
        self.assertEqual(node.latest_block_hash, _h(9))

    def test_stops_at_genesis(self):
        node = _node(head=_h(2))
        with mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=ZERO32):
            self.assertIsNone(_walk_back(node, _h(2), _h(9)))
        self.assertEqual(node.latest_block_hash, _h(2))

    def test_no_move_without_an_earlier_block(self):
        node = _node(head=_h(2))
        with mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=None):
            self.assertIsNone(_walk_back(node, _h(2), None))
            self.assertIsNone(_walk_back(node, _h(2), _h(2)))
        self.assertEqual(node.latest_block_hash, _h(2))

    def test_keeps_a_newer_head(self):
        node = _node(head=_h(3))
        with mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=_h(1)):
            self.assertIsNone(_walk_back(node, _h(2), _h(9)))
        self.assertEqual(node.latest_block_hash, _h(3))


class TestPreviousHashLocal(unittest.TestCase):
    def setUp(self):
        self.node = Node()
        genesis = create_genesis_block(self.node, validator_public_key=_h(7))
        genesis.accounts.update_trie(self.node)
        generate_block_nonce(block=genesis, difficulty=1)
        self.genesis_hash = get_block_expr(genesis).hash()
        self.exprs, _ = resolve_inner_exprs(self.node, get_block_expr(genesis))

    def test_reads_the_parent_hash_from_hot_storage(self):
        for expr in self.exprs:
            put_expr_in_hot_storage(self.node, expr)
        self.assertEqual(get_previous_block_hash_local(self.node, self.genesis_hash), ZERO32)

    def test_returns_none_and_does_not_fetch_when_the_header_is_missing(self):
        with mock.patch(
            "astreum.storage.exprs.cascade.get_expr_from_network"
        ) as network:
            self.assertIsNone(get_previous_block_hash_local(self.node, self.genesis_hash))
        network.assert_not_called()


class TestWorkerLoop(unittest.TestCase):
    """Run the worker loop until its first pass ends."""

    KEY = b"\x07" * 32

    def _run(self, *, validator_side_effect, head=_h(2), last_good=_h(1), previous_hash=None):
        """Start with ``last_good`` as the head. The first pass moves it to ``head``."""
        node = _node(head=last_good)
        node.config = SimpleNamespace(validation_public_key_bytes=self.KEY)
        node._validation_stop_event = threading.Event()
        parent = SimpleNamespace(timestamp=500, height=2, accounts_hash=_h(0x0A), expr_id=head)
        draws = []

        def draw(*args, **kwargs):
            draws.append((args, kwargs))
            return validator_side_effect(args, kwargs)

        def move_head(_node):
            node.latest_block_hash = head

        def stop_after_sleep(_seconds):
            node._validation_stop_event.set()

        with mock.patch.object(worker_module, "flush_ads", side_effect=move_head), \
             mock.patch.object(worker_module, "block_holder", return_value=None), \
             mock.patch.object(worker_module, "get_block_from_storage", return_value=parent), \
             mock.patch.object(worker_module, "_fetch_new_nodes"), \
             mock.patch.object(worker_module, "current_validator", side_effect=draw), \
             mock.patch.object(worker_module, "get_previous_block_hash_local", return_value=previous_hash), \
             mock.patch.object(worker_module.time, "sleep", side_effect=stop_after_sleep):
            make_validation_worker(node)()
        return node, draws, parent

    def test_failed_load_walks_back_to_the_parent(self):
        def boom(args, kwargs):
            raise ValueError("state missing")

        node, _, _ = self._run(validator_side_effect=boom, previous_hash=_h(1))
        self.assertEqual(node.latest_block_hash, _h(1))

    def test_failed_load_without_header_walks_back_to_last_good_head(self):
        def boom(args, kwargs):
            raise ValueError("state missing")

        node, _, _ = self._run(validator_side_effect=boom, previous_hash=None, last_good=_h(5))
        self.assertEqual(node.latest_block_hash, _h(5))

    def test_other_validator_does_not_walk_back(self):
        def other(args, kwargs):
            return b"\x08" * 32, mock.Mock()

        node, _, _ = self._run(validator_side_effect=other, previous_hash=_h(1))
        self.assertEqual(node.latest_block_hash, _h(2))


class TestWorkerBatchFetch(unittest.TestCase):
    """The worker fetches the new nodes of the head in batches before it reads them."""

    KEY = b"\x07" * 32
    ACCOUNTS_ROOT = _h(0x0A)
    STORAGE_ROOT = _h(0x0B)
    STORAGE_BLOCK = _h(0x0C)

    def _run(self, *, network_fails=False, parent_timestamp=500, now=1000.2, walk_error=None):
        """Run one pass of the loop as the scheduled validator.

        The record walk ends the pass by raising ``UnresolvedRecordExpr``.

        Returns:
            The list of calls in order, as ``(name, args)``.
        """
        head = _h(2)
        node = _node(head=head)
        node.nonce_time_ms = 500
        node.config = SimpleNamespace(validation_public_key_bytes=self.KEY)
        node._validation_stop_event = threading.Event()
        node._validation_transaction_queue = mock.Mock()
        parent = SimpleNamespace(
            timestamp=parent_timestamp,
            height=2,
            accounts_hash=self.ACCOUNTS_ROOT,
            expr_id=head,
            bloom_hash=ZERO32,
        )
        storage_account = SimpleNamespace(
            data=SimpleNamespace(root_hash=self.STORAGE_ROOT, root_block=self.STORAGE_BLOCK)
        )
        new_block = SimpleNamespace(
            height=3,
            transactions=[],
            receipts=[],
            pending_exprs=[],
            accounts=SimpleNamespace(get_account=lambda *a, **k: storage_account),
        )
        calls = []

        def fetch(*args, **kwargs):
            calls.append(("fetch", args))

        def draw(*args, **kwargs):
            calls.append(("draw", args))
            self.draw_kwargs = kwargs
            return self.KEY, mock.Mock()

        def walk(*args, **kwargs):
            calls.append(("walk", args))
            raise walk_error or worker_module.UnresolvedRecordExpr(_h(0x0D))

        def stop_after_sleep(_seconds):
            node._validation_stop_event.set()

        with mock.patch.object(worker_module, "flush_ads"), \
             mock.patch.object(worker_module, "block_holder", return_value="holder"), \
             mock.patch.object(worker_module, "get_block_from_storage", return_value=parent), \
             mock.patch.object(
                 worker_module,
                 "_fetch_new_nodes",
                 side_effect=None if network_fails else fetch,
                 **({"wraps": worker_module._fetch_new_nodes} if network_fails else {}),
             ), \
             mock.patch(
                 "astreum.consensus.block.verify.transactions.get_radix_all_from_network",
                 side_effect=OSError("no peer"),
             ), \
             mock.patch.object(worker_module, "current_validator", side_effect=draw), \
             mock.patch.object(worker_module, "_next_signed_transaction", side_effect=worker_module.Empty), \
             mock.patch.object(worker_module, "create_block", return_value=new_block), \
             mock.patch.object(worker_module, "generate_initial_storage_record", side_effect=walk), \
             mock.patch("astreum.consensus.block.encoding.expr.get_block_expr"), \
             mock.patch.object(worker_module, "_walk_back", return_value=None) as walk_back, \
             mock.patch.object(worker_module.time, "time", return_value=now), \
             mock.patch.object(worker_module.time, "sleep", side_effect=stop_after_sleep):
            make_validation_worker(node)()
        self.walk_back = walk_back
        return calls

    def test_accounts_batch_comes_before_the_draw(self):
        calls = self._run()
        self.assertEqual([name for name, _ in calls], ["fetch", "draw", "fetch", "walk"])
        self.assertEqual(
            calls[0][1],
            (mock.ANY, self.ACCOUNTS_ROOT, ACCOUNT_SHAPE, _h(2), "holder"),
        )

    def test_storage_batch_comes_before_the_record_walk(self):
        calls = self._run()
        self.assertEqual(
            calls[2][1],
            (mock.ANY, self.STORAGE_ROOT, STORAGE_RECORD_SHAPE, self.STORAGE_BLOCK, "holder"),
        )

    def test_the_draw_is_for_the_time_the_block_will_carry(self):
        # 1000.2 s now, 0.5 s to find the nonce, 2 s of spacing: ceil(1002.7).
        self._run()
        self.assertEqual(self.draw_kwargs["target_time"], 1003)
        self.assertEqual(self.draw_kwargs["contact"], "holder")

    def test_the_draw_time_is_at_least_one_spacing_after_the_parent(self):
        self._run(parent_timestamp=2000)
        self.assertEqual(self.draw_kwargs["target_time"], 2002)

    def test_a_failed_batch_does_not_stop_the_build(self):
        calls = self._run(network_fails=True)
        self.assertEqual([name for name, _ in calls], ["draw", "walk"])

    def test_an_unresolved_expr_walks_back(self):
        self._run()
        self.walk_back.assert_called_once()

    def test_an_unreadable_storage_node_walks_back_and_keeps_the_thread(self):
        error = ValueError("could not retrieve Radix node expr from storage")
        self._run(walk_error=error)
        self.walk_back.assert_called_once()


if __name__ == "__main__":
    unittest.main()
