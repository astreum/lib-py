"""The verify worker never moves the head to a lower block."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.consensus.verification import worker as verify_worker  # noqa: E402


def _h(n: int) -> bytes:
    return bytes([n]) * 32


def _fork(peers=("p",)):
    root = _h(0)
    return SimpleNamespace(
        malicious_block_hash=None,
        root=root,
        header_verified_up_to=root,
        transactions_verified_up_to=root,
        peers=set(peers),
    )


def _node(head, head_height, forks):
    blocks = {}
    node = SimpleNamespace(
        config=SimpleNamespace(
            verification_max_stale_seconds=-1, verification_max_future_skew=-1
        ),
        latest_block_hash=head,
        latest_block=SimpleNamespace(expr_id=head, height=head_height) if head else None,
        latest_block_lock=threading.Lock(),
        forks=forks,
        forks_lock=threading.Lock(),
        block_spacing=5,
    )
    return node, blocks


def _select(node, heights):
    def load(astreum_node, block_hash, contact=None, **kwargs):
        if block_hash not in heights:
            raise ValueError("missing")
        return SimpleNamespace(
            expr_id=block_hash, height=heights[block_hash], timestamp=int(time.time())
        )

    with mock.patch.object(verify_worker, "get_block_from_storage", side_effect=load), \
         mock.patch.object(verify_worker, "block_holder", return_value=None):
        verify_worker._select_latest_block(node)


class TestSelectLatestBlock(unittest.TestCase):
    def test_head_does_not_move_to_a_lower_fork(self):
        node, _ = _node(_h(5), 5, {_h(3): _fork()})
        _select(node, {_h(3): 3, _h(5): 5})
        self.assertEqual(node.latest_block_hash, _h(5))

    def test_head_moves_to_a_higher_fork(self):
        node, _ = _node(_h(5), 5, {_h(7): _fork()})
        _select(node, {_h(7): 7, _h(5): 5})
        self.assertEqual(node.latest_block_hash, _h(7))
        self.assertEqual(node.latest_block.height, 7)

    def test_equal_height_keeps_the_lower_hash(self):
        node, _ = _node(_h(5), 5, {_h(4): _fork()})
        _select(node, {_h(4): 5, _h(5): 5})
        self.assertEqual(node.latest_block_hash, _h(4))

    def test_head_height_is_read_from_storage_when_the_block_is_not_loaded(self):
        node, _ = _node(_h(5), 5, {_h(3): _fork()})
        node.latest_block = None
        _select(node, {_h(3): 3, _h(5): 5})
        self.assertEqual(node.latest_block_hash, _h(5))

    def test_head_moves_when_the_current_head_cannot_be_read(self):
        node, _ = _node(_h(5), 5, {_h(3): _fork()})
        node.latest_block = None
        _select(node, {_h(3): 3})
        self.assertEqual(node.latest_block_hash, _h(3))

    def test_head_changed_during_selection_is_kept(self):
        node, _ = _node(_h(5), 5, {_h(7): _fork()})
        real = verify_worker._current_head_height

        def moved(n, head):
            height = real(n, head)
            n.latest_block_hash = _h(6)
            return height

        with mock.patch.object(verify_worker, "_current_head_height", side_effect=moved):
            _select(node, {_h(7): 7, _h(5): 5})
        self.assertEqual(node.latest_block_hash, _h(6))


if __name__ == "__main__":
    unittest.main()
