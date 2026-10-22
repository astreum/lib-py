import sys
import socket
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
from astreum.crypto.bloom_search.block_search import find_block_by_height  # noqa: E402
from astreum.communication.node import connect_node
from astreum.communication.disconnect import disconnect_node


class TestBlockSearch(unittest.TestCase):
    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_find_block_by_height_after_mining(self) -> None:
        port = self._get_free_port()
        node = Node(
            config={
                "port": port,
                "default_seed": None,
                "additional_seeds": [],
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "verbose": False,
                "logger_name": "block_search_test",
            }
        )
        connect_node(node)
        try:
            secret_key = Ed25519PrivateKey.generate()
            validate_blockchain(node, secret_key)

            # Wait for enough blocks to be mined (~7s per block at diff 1).
            deadline = time.time() + 90
            while time.time() < deadline:
                latest = node.latest_block
                if latest is not None and int(latest.height or 0) >= 10:
                    break
                time.sleep(0.5)
            else:
                self.fail("node did not reach height 10 within 90 seconds")

            block = find_block_by_height(node, starting_block=node.latest_block, target_height=10)
            self.assertIsNotNone(block, "find_block_by_height returned None for height 10")
            self.assertEqual(int(block.height), 10,
                             f"expected height 10, got {block.height}")

            # Also verify a few other heights while we're here
            latest = node.latest_block
            for h in (0, 1, 5, 9):
                b = find_block_by_height(node, starting_block=latest, target_height=h)
                self.assertIsNotNone(b, f"find_block_by_height returned None for height {h}")
                self.assertEqual(int(b.height), h,
                                 f"expected height {h}, got {b.height}")

            # Verify unreachable height returns None
            unreachable = latest.height + 1
            self.assertIsNone(
                find_block_by_height(node, starting_block=latest, target_height=unreachable),
                f"expected None for unreachable height {unreachable}",
            )
        finally:
            disconnect_node(node)


if __name__ == "__main__":
    unittest.main()
