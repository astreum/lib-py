"""A validator advertises the records a block committed once it is adopted.

The ids wait in ``node.pending_ads`` after the block is created. They go out
once, when a block has been built on it; blocks
that lost a fork are dropped, and the queue is bounded.
"""

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.validation import ads  # noqa: E402
from astreum.consensus.validation.ads import (  # noqa: E402
    MAX_PENDING_AD_AGE,
    MAX_PENDING_ADS,
    flush_ads,
    init_pending_ads,
    queue_ads,
)
from astreum.expression import RESOLUTION_SINGLE  # noqa: E402

ADVERTISE = "astreum.storage.advertisements.advertise_exprs"
LOAD_BLOCK = "astreum.consensus.block.encoding.decode.get_block_from_storage"


def _hash(n: int) -> bytes:
    return bytes([n]) * 32


def _ids(n: int, count: int = 2) -> list[bytes]:
    return [bytes([n, i]) + b"\x00" * 30 for i in range(count)]


def _block(height, block_hash, parent=None):
    return SimpleNamespace(
        height=height,
        expr_id=block_hash,
        previous_block=parent,
        previous_block_hash=parent.expr_id if parent is not None else _hash(0),
    )


def _chain(top):
    """Blocks at heights 1..top, each linked to its parent in memory."""
    blocks = {}
    parent = None
    for height in range(1, top + 1):
        parent = _block(height, _hash(height), parent)
        blocks[height] = parent
    return blocks


def _node(head=None):
    logger = SimpleNamespace(
        debug=lambda *a, **k: None,
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    node = SimpleNamespace(
        logger=logger, latest_block=head, latest_block_lock=threading.Lock()
    )
    init_pending_ads(node)
    return node


def _advertise_ok(node, entries):
    return [entry[0] for entry in entries], None


class TestQueue(unittest.TestCase):
    def test_queued_ids_are_not_sent(self):
        chain = _chain(5)
        node = _node(head=chain[5])
        queue_ads(node, 5, _hash(5), _ids(5))
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            self.assertEqual(flush_ads(node), [])  # the head is B itself
        advertise.assert_not_called()
        self.assertEqual(len(node.pending_ads), 1)

    def test_nothing_queued_for_a_block_without_records(self):
        node = _node()
        queue_ads(node, 5, _hash(5), [])
        self.assertEqual(len(node.pending_ads), 0)

    def test_queue_is_created_on_first_use(self):
        node = SimpleNamespace()
        queue_ads(node, 5, _hash(5), _ids(5))
        self.assertEqual(len(node.pending_ads), 1)

    def test_queue_is_bounded_and_drops_the_oldest(self):
        node = _node()
        for height in range(1, MAX_PENDING_ADS + 4):
            queue_ads(node, height, _hash(height), _ids(height))
        self.assertEqual(len(node.pending_ads), MAX_PENDING_ADS)
        self.assertEqual(node.pending_ads[0].height, 4)


class TestFlush(unittest.TestCase):
    def test_sent_once_after_a_block_is_built_on_it(self):
        chain = _chain(6)
        node = _node(head=chain[6])
        ids = _ids(5)
        queue_ads(node, 5, _hash(5), ids)
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            self.assertEqual(flush_ads(node), ids)
            self.assertEqual(flush_ads(node), [])
        advertise.assert_called_once()
        entries = advertise.call_args.args[1]
        self.assertEqual(entries, [(i, RESOLUTION_SINGLE, None) for i in ids])
        self.assertEqual(len(node.pending_ads), 0)

    def test_sent_when_the_head_is_several_blocks_past(self):
        chain = _chain(9)
        node = _node(head=chain[9])
        queue_ads(node, 5, _hash(5), _ids(5))
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            flush_ads(node)
        advertise.assert_called_once()

    def test_each_block_is_sent_separately_in_order(self):
        chain = _chain(8)
        node = _node(head=chain[8])
        queue_ads(node, 5, _hash(5), _ids(5))
        queue_ads(node, 6, _hash(6), _ids(6))
        queue_ads(node, 8, _hash(8), _ids(8))  # the head: still waiting
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            flush_ads(node)
        sent = [[e[0] for e in call.args[1]] for call in advertise.call_args_list]
        self.assertEqual(sent, [_ids(5), _ids(6)])
        self.assertEqual([e.height for e in node.pending_ads], [8])

    def test_a_block_that_lost_a_fork_is_dropped_unsent(self):
        chain = _chain(6)
        node = _node(head=chain[6])
        queue_ads(node, 5, _hash(99), _ids(5))  # not the block at height 5
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            self.assertEqual(flush_ads(node), [])
        advertise.assert_not_called()
        self.assertEqual(len(node.pending_ads), 0)

    def test_nothing_happens_without_a_head(self):
        node = _node(head=None)
        queue_ads(node, 5, _hash(5), _ids(5))
        with mock.patch(ADVERTISE, side_effect=_advertise_ok) as advertise:
            self.assertEqual(flush_ads(node), [])
        advertise.assert_not_called()
        self.assertEqual(len(node.pending_ads), 1)

    def test_ancestors_missing_in_memory_are_loaded_from_storage(self):
        chain = _chain(6)
        head = _block(6, _hash(6), parent=None)
        head.previous_block_hash = _hash(5)
        node = _node(head=head)
        queue_ads(node, 5, _hash(5), _ids(5))
        with mock.patch(LOAD_BLOCK, return_value=chain[5]) as load, mock.patch(
            ADVERTISE, side_effect=_advertise_ok
        ) as advertise:
            flush_ads(node)
        load.assert_called_once()
        self.assertEqual(load.call_args.args[1], _hash(5))
        advertise.assert_called_once()

    def test_an_unplaceable_block_waits_then_is_dropped(self):
        head = _block(6, _hash(6), parent=None)
        node = _node(head=head)
        queue_ads(node, 5, _hash(5), _ids(5))
        with mock.patch(LOAD_BLOCK, side_effect=KeyError("missing")), mock.patch(
            ADVERTISE, side_effect=_advertise_ok
        ) as advertise:
            flush_ads(node)
            self.assertEqual(len(node.pending_ads), 1)
            node.latest_block = _block(5 + MAX_PENDING_AD_AGE + 1, _hash(50))
            flush_ads(node)
        advertise.assert_not_called()
        self.assertEqual(len(node.pending_ads), 0)

    def test_advertise_warnings_do_not_lose_the_ids_sent(self):
        chain = _chain(6)
        node = _node(head=chain[6])
        ids = _ids(5)
        queue_ads(node, 5, _hash(5), ids)
        with mock.patch(ADVERTISE, return_value=(ids[:1], "1 advertisement(s) failed")):
            self.assertEqual(flush_ads(node), ids[:1])
        self.assertEqual(len(node.pending_ads), 0)


class TestHashOfBlock(unittest.TestCase):
    def test_a_block_without_a_stored_id_is_hashed_from_its_expr(self):
        with mock.patch(
            "astreum.consensus.block.encoding.expr.get_block_expr",
            return_value=SimpleNamespace(hash=lambda: _hash(7)),
        ):
            self.assertEqual(ads._block_hash(SimpleNamespace(expr_id=b"\x00" * 32)), _hash(7))


if __name__ == "__main__":
    unittest.main()
