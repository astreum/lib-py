import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node
from astreum.utils.config import NodeConfig
from astreum.communication.node import connect_node
from cryptography.hazmat.primitives import serialization


class TestNodeInitialization(unittest.TestCase):
    def test_init_with_empty_config(self):
        empty_config: dict = {}

        node = Node(empty_config)
        self.assertIsInstance(node.config, NodeConfig)
        self.assertEqual(empty_config, {})
        self.assertEqual(node.config.chain_id, 0)
        self.assertTrue(node.config.logging_enabled)

        derived_settings = [
            ("chain_name", node.config.chain),
            ("chain_id", node.config.chain_id),
            ("hot_storage_limit", node.config.hot_storage_limit),
            ("cold_storage_limit", node.config.cold_storage_limit),
            ("cold_storage_path", node.config.cold_storage_path or "<disabled>"),
        ]

        for key, value in derived_settings:
            print(f"{key}: {value}")

        connect_node(node)

        relay_public_key_bytes = (
            node.relay_public_key.public_bytes(  # type: ignore[attr-defined]
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
            if getattr(node, "relay_public_key", None)
            else None
        )
        network_settings = [
            ("relay_public_key", relay_public_key_bytes.hex() if relay_public_key_bytes else "<unavailable>"),
            ("port", node.config.port),
        ]

        for key, value in network_settings:
            print(f"{key}: {value}")

        for socket_attr in ("socket",):
            sock = getattr(node, socket_attr, None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
