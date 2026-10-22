"""Test that a validator node can resume block production after a full restart.

Unlike ``tests.node.test_validation_resume`` (which reuses the same ``Node``
instance), this test creates a **fresh** ``Node`` for the resume phase,
verifying that cold-storage persistence alone is sufficient to continue
validation from where it left off.

All on-disk state lives in temporary directories that are cleaned up
automatically when the test finishes.
"""

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.communication.node import connect_node
from astreum.communication.disconnect import disconnect_node


class TestValidationResume(unittest.TestCase):
    """Validate, go offline, resume with a fresh Node instance."""

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_resume_validation_after_restart(self) -> None:
        cold_dir = tempfile.TemporaryDirectory(prefix="astreum_cold_")
        secret_key = Ed25519PrivateKey.generate()

        base_config = {
            "port": self._get_free_port(),
            "default_seed": None,
            "additional_seeds": [],
            "cold_storage_path": cold_dir.name,
            "cold_storage_scale": "KB",
            "storage_index_interval": 1,
            "atom_fetch_interval": 1,
            "logging_enabled": False,
            "verbose": False,
        }

        # ── Phase 1: validate (no transactions), produce blocks ────────
        node = Node(config=dict(base_config))
        connect_node(node)
        try:
            validate_blockchain(node, secret_key)
            time.sleep(15)

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
        finally:
            disconnect_node(node)

        # ── Phase 2: fresh Node, resume from cold storage ──────────────
        resume_config = dict(base_config)
        resume_config["port"] = self._get_free_port()
        resume_config["latest_block_hash"] = "0x" + saved_hash.hex()
        resume_config["verbose"] = True
        resume_config["logging_enabled"] = True

        node = Node(config=resume_config)
        connect_node(node)
        try:
            validate_blockchain(node, secret_key)

            loaded_height = node.latest_block.height
            self.assertEqual(
                loaded_height, height_before,
                "resumed node should load the same latest block height",
            )
            self.assertEqual(
                node.latest_block_hash, saved_hash,
                "resumed node should load the same latest block hash",
            )
            print(
                f"Phase 2 resume: height={loaded_height}, "
                f"hash={node.latest_block_hash.hex()[:16]}..."
            )

            # Wait for the validation worker to produce at least one more block
            deadline = time.time() + 30
            while time.time() < deadline:
                if node.latest_block.height > height_before:
                    break
                # Debug: check if validation thread is alive
                vt = getattr(node, "consensus_validation_thread", None)
                if vt and not vt.is_alive():
                    self.fail(
                        f"validation thread died; "
                        f"latest_block_hash={node.latest_block_hash!r}, "
                        f"block_spacing={getattr(node, 'block_spacing', None)}"
                    )
                time.sleep(0.5)

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

        cold_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
