import socket
import sys
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


class TestValidationProgress(unittest.TestCase):
    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_block_production_every_10s(self) -> None:
        port = self._get_free_port()
        node = Node(
            config={
                "port": port,
                "default_seed": None,
                "additional_seeds": [],
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "verbose": True,
            }
        )
        connect_node(node)
        try:
            secret_key = Ed25519PrivateKey.generate()
            validate_blockchain(node, secret_key)

            deadline = time.time() + 30
            prev_block_count = 0
            prev_hash = node.latest_block_hash
            while time.time() < deadline:
                time.sleep(10)

                block = node.latest_block
                current_count = block.height if block else -1

                current = node.latest_block_hash
                print(
                    f"Check: height={current_count} "
                    f"prev={prev_hash.hex()[:16] if prev_hash else 'None'} "
                    f"current={current.hex()[:16] if current else 'None'}"
                )
                if current == prev_hash:
                    self.fail(
                        f"no new block produced in 10-second window "
                        f"(hash stayed at {prev_hash.hex()[:16] if prev_hash else 'None'})"
                    )
                prev_hash = current
                prev_block_count = current_count
        finally:
            disconnect_node(node)
