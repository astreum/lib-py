import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.communication.node import connect_node
from astreum.communication.disconnect import disconnect_node


class TestValidationResume(unittest.TestCase):
    """Test that a validator node can resume block production from cold
    storage after a shutdown/restart, using the latest block hash as the
    anchor to skip genesis and continue from where it left off."""

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_validation_resume_from_cold_storage(self) -> None:
        cold_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        secret_key = Ed25519PrivateKey.generate()
        port = self._get_free_port()
        base_config = {
            "port": port,
            "default_seed": None,
            "additional_seeds": [],
            "cold_storage_path": cold_dir.name,
            "cold_storage_scale": "KB",
            "storage_index_interval": 1,
            "atom_fetch_interval": 1,
            "verbose": False,
        }

        # ── Phase 1: first run, fresh genesis, produce blocks ─────────
        node = Node(config=dict(base_config))
        connect_node(node)
        try:
            validate_blockchain(node, secret_key)

            # Produce blocks for ~15 seconds (block_spacing is a constant 2,
            # so genesis + ~6-7 blocks in that window)
            time.sleep(15)
        finally:
            disconnect_node(node)

        # Read the saved state only after the validation worker has stopped.
        # disconnect_node waits 1 second for the worker. A worker that is
        # still building a block can commit it after that wait.
        node.consensus_validation_thread.join(timeout=30)
        self.assertFalse(
            node.consensus_validation_thread.is_alive(),
            "validation worker should stop after disconnect",
        )
        saved_hash = node.latest_block_hash
        saved_block = node.latest_block
        self.assertIsNotNone(
            saved_hash, "node should have a latest_block_hash after genesis"
        )
        self.assertIsNotNone(
            saved_block, "node should have a latest_block after genesis"
        )
        height_before = saved_block.height
        self.assertGreater(
            height_before, 0,
            f"expected at least 1 block (genesis), got height={height_before}",
        )
        print(
            f"Phase 1 complete: height={height_before}, "
            f"hash={saved_hash.hex()[:16]}..."
        )

        # ── Phase 2: same node, resume from cold storage ──────────────
        # latest_block_hash was persisted on the instance attribute during
        # phase 1.  With the communication_setup fix, connect() no longer
        # wipes it, so genesis is skipped and block production continues.
        connect_node(node)
        try:
            validate_blockchain(node, secret_key)

            # Verify latest block loaded from storage (not re-created genesis)
            loaded_height = node.latest_block.height
            self.assertGreaterEqual(
                loaded_height, height_before,
                "resumed node should load at least the same latest block height",
            )
            self.assertEqual(
                node.latest_block_hash, saved_hash,
                "resumed node should load the same latest block hash",
            )

            print(
                f"Phase 2 resume: height={loaded_height}, "
                f"hash={node.latest_block_hash.hex()[:16]}..."
            )

            # Let the validation worker produce at least one more block
            time.sleep(10)

            resumed_height = node.latest_block.height
            self.assertGreater(
                resumed_height, height_before,
                f"block production should continue after resume "
                f"(before={height_before}, after={resumed_height})",
            )
            print(
                f"Phase 2 continued: height={resumed_height}, "
                f"hash={node.latest_block_hash.hex()[:16]}..."
            )
        finally:
            disconnect_node(node)
            # The worker can still write to cold storage after disconnect.
            node.consensus_validation_thread.join(timeout=30)

        cold_dir.cleanup()
