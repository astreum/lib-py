"""Cost tests for ``find_block_by_height`` across era boundaries.

The search crosses eras through the era heads, so a lookback must not cost one
parent fetch per block. These tests count the fetches without mining a chain.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.crypto.bloom_search import block_search  # noqa: E402
from astreum.crypto.bloom_search.search import ERA_SIZE  # noqa: E402

TREE = b"\x01" * 32


class FakeBlock:
    """A block with only the fields the height search reads."""

    def __init__(self, height: int, *, tree: bool = True) -> None:
        self.height = height
        self.expr_id = _hash(height)
        self.previous_block_hash = _hash(height - 1)
        self.previous_block = None
        self.bloom_hash = TREE if tree else None
        self.previous_era_hash = b"\x00" * 32


def _hash(height: int) -> bytes:
    """Hash of the fake block at *height*."""
    return height.to_bytes(32, "big")


class TestBlockSearchLookback(unittest.TestCase):
    """How many parent fetches a lookback costs."""

    def _chain(self, first: int, last: int, *, tree: bool = True) -> dict:
        return {h: FakeBlock(h, tree=tree) for h in range(first, last + 1)}

    def _search(self, blocks: dict, start: int, target: int) -> tuple:
        """Run the search over a fake chain.

        The bloom tree is stubbed as a window over the era: a descent reaches
        any offset that has been mined in the era of the block it starts from.

        Args:
            blocks: Height to block map.
            start: Height to start from.
            target: Height to find.

        Returns:
            Tuple of (found block, list of fetched parent heights).
        """
        by_hash = {block.expr_id: block for block in blocks.values()}
        fetches: list[int] = []

        def fake_previous(node, block):
            fetches.append(block.height)
            return blocks.get(block.height - 1)

        def fake_leaf(node, root_hash, offset, block_hash=None):
            block = by_hash.get(block_hash)
            if block is None:
                return None
            if offset > block.height % ERA_SIZE:
                return None
            era_base = (block.height // ERA_SIZE) * ERA_SIZE
            return _hash(era_base + offset)

        def fake_get(node=None, block_hash=None, **kwargs):
            if node is None:
                node = kwargs.get("astreum_node")
            return by_hash.get(block_hash)

        originals = (
            block_search.get_previous_block,
            block_search._storage_find_leaf,
            block_search.get_block_from_storage,
        )
        block_search.get_previous_block = fake_previous
        block_search._storage_find_leaf = fake_leaf
        block_search.get_block_from_storage = fake_get
        try:
            found = block_search.find_block_by_height(
                None, starting_block=blocks[start], target_height=target
            )
        finally:
            (
                block_search.get_previous_block,
                block_search._storage_find_leaf,
                block_search.get_block_from_storage,
            ) = originals
        return found, fetches

    def test_lookback_across_two_eras_skips_eras(self) -> None:
        """A two-era lookback costs one parent fetch per era, not per block."""
        blocks = self._chain(1024, 3071)
        found, fetches = self._search(blocks, start=3071, target=1024)
        self.assertIsNotNone(found)
        self.assertEqual(found.height, 1024)
        self.assertLessEqual(
            len(fetches),
            4,
            f"lookback cost {len(fetches)} fetches for two eras",
        )

    def test_same_era_lookup_costs_no_parent_fetch(self) -> None:
        blocks = self._chain(2048, 3071)
        found, fetches = self._search(blocks, start=3071, target=3000)
        self.assertEqual(found.height, 3000)
        self.assertEqual(fetches, [])

    def test_missing_tree_falls_back_to_the_parent_walk(self) -> None:
        """Without a bloom tree the search walks one era, not the distance."""
        blocks = self._chain(1024, 3071, tree=False)
        found, fetches = self._search(blocks, start=3071, target=2047)
        self.assertEqual(found.height, 2047)
        self.assertLessEqual(len(fetches), ERA_SIZE)

    def test_target_above_start_returns_none(self) -> None:
        blocks = self._chain(1024, 3071)
        found, fetches = self._search(blocks, start=2048, target=3000)
        self.assertIsNone(found)
        self.assertEqual(fetches, [])


if __name__ == "__main__":
    unittest.main()
