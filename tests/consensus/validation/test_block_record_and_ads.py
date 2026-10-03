"""What the block after N commits, on a chain built by the real validation worker.

- N's record covers the tries N's header names by root hash, including the
  storage trie as of N, and the validator's records table lists them.
- The record ids are advertised only once a block has been built on N.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
for path in (SRC_DIR, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from astreum.communication.disconnect import disconnect_node  # noqa: E402
from astreum.communication.node import connect_node  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.consensus.constants import STORAGE_ADDRESS  # noqa: E402
from astreum.consensus.models.accounts import Accounts  # noqa: E402
from astreum.consensus.validation import node as validation_node  # noqa: E402
from astreum.consensus.validation.ads import flush_ads  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.expression import ZERO32  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.storage.exprs import get_expr_from_local_storage  # noqa: E402
from astreum.storage.records import get_record_from_cold_storage  # noqa: E402
from tests.consensus.validation.test_verify_stake_halving import (  # noqa: E402
    _free_port,
    _genesis_with_stake,
)


def _slot_ids(blob):
    chunks = (blob[i : i + 32] for i in range(0, len(blob), 32))
    return [chunk for chunk in chunks if chunk != ZERO32]


class TestBlockRecordAndAds(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cold_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        secret_key = Ed25519PrivateKey.generate()
        cls.node = Node(
            config={
                "port": _free_port(),
                "default_seed": None,
                "additional_seeds": [],
                "cold_storage_path": cls.cold_dir.name,
                "cold_storage_scale": "KB",
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "logging_enabled": False,
                "verbose": False,
            }
        )
        connect_node(cls.node)
        try:
            with mock.patch.object(
                validation_node, "create_genesis_block", _genesis_with_stake
            ):
                validate_blockchain(cls.node, secret_key)

            deadline = time.time() + 900
            while time.time() < deadline and cls.node.latest_block.height < 2:
                time.sleep(0.5)
            cls.node._validation_stop_event.set()
            cls.node.consensus_validation_thread.join(timeout=60)
            if cls.node.latest_block.height < 1:
                raise AssertionError("worker did not produce any block")

            # blocks[0] is height 1, genesis excluded, each with its parent.
            chain = []
            block_hash = cls.node.latest_block_hash
            while True:
                block = get_block_from_storage(
                    astreum_node=cls.node, block_hash=block_hash
                )
                if block.previous_block_hash == ZERO32:
                    break
                chain.append(block)
                block_hash = block.previous_block_hash
            chain.reverse()
            for block in chain:
                block.previous_block = get_block_from_storage(
                    astreum_node=cls.node, block_hash=block.previous_block_hash
                )
            cls.chain = chain
        except BaseException:
            disconnect_node(cls.node)
            cls.cold_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        disconnect_node(cls.node)
        cls.cold_dir.cleanup()

    def test_block_record_covers_the_previous_blocks_tries(self) -> None:
        for block in self.chain:
            previous = block.previous_block
            blob = get_record_from_cold_storage(self.node, block.previous_block_hash)
            self.assertIsNotNone(blob, f"height {block.height}: no records-table entry")
            slots = _slot_ids(blob)
            self.assertIn(previous.accounts_hash, slots, f"height {block.height}")
            if previous.bloom_hash != ZERO32:
                self.assertIn(previous.bloom_hash, slots, f"height {block.height}")
            for slot_id in slots:
                self.assertIsNotNone(
                    get_expr_from_local_storage(self.node, slot_id),
                    f"height {block.height}: V does not hold slot {slot_id.hex()[:12]}",
                )

    def test_block_record_covers_the_storage_trie_as_of_the_previous_block(self) -> None:
        for block in self.chain:
            previous = block.previous_block
            storage_root = (
                Accounts(root_hash=previous.accounts_hash)
                .get_account(STORAGE_ADDRESS, self.node)
                .data_hash
            )
            if storage_root == ZERO32:
                continue  # nothing registered yet
            slots = _slot_ids(
                get_record_from_cold_storage(self.node, block.previous_block_hash)
            )
            self.assertIn(storage_root, slots, f"height {block.height}")

    def test_record_ids_are_advertised_only_once_the_block_is_built_on(self) -> None:
        head = self.chain[-1]
        queued = [e for e in self.node.pending_ads if e.height == head.height]
        self.assertEqual(len(queued), 1)
        self.assertIn(head.previous_block_hash, queued[0].ids)
        self.assertNotIn(head.previous_block_hash, self.node.storage_index)

        flush_ads(self.node)
        for block in self.chain[:-1]:
            self.assertIn(block.previous_block_hash, self.node.storage_index)
        self.assertNotIn(head.previous_block_hash, self.node.storage_index)
        self.assertEqual([e.height for e in self.node.pending_ads], [head.height])


if __name__ == "__main__":
    unittest.main()
