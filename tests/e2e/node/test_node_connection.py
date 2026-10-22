from __future__ import annotations

import contextlib
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node  # noqa: E402
from astreum.communication.node import connect_node  # noqa: E402
from astreum.communication.models.peer import get_peer  # noqa: E402


class TestNodeConnection(unittest.TestCase):
    def setUp(self) -> None:
        self._nodes: list[Node] = []

    def tearDown(self) -> None:
        for node in self._nodes:
            self._shutdown_node(node)

    def _register_node(self, node: Node) -> Node:
        self._nodes.append(node)
        return node

    @staticmethod
    def _shutdown_node(node: Node) -> None:
        for attr in ("socket",):
            sock = getattr(node, attr, None)
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_connection(self) -> None:
        node_a_port = self._get_free_port()
        node_a = self._register_node(
            Node({"port": node_a_port, "default_seed": None})
        )

        connect_node(node_a)

        self.assertTrue(node_a.is_connected)
        self.assertGreater(node_a.config.port, 0)
        print(f"node_a port={node_a.config.port}")

        bootstrap_host = "127.0.0.1"
        bootstrap_port = node_a.config.port
        node_b_port = self._get_free_port()

        node_b = self._register_node(
            Node(
                {
                    "port": node_b_port,
                    "default_seed": None,
                    "additional_seeds": [f"{bootstrap_host}:{bootstrap_port}"],
                }
            )
        )

        connect_node(node_b)

        self.assertTrue(node_b.is_connected)
        print(
            f"node_b port={node_b.config.port} "
            f"seed={bootstrap_host}:{bootstrap_port}"
        )

        node_a_peer_key = getattr(node_b, "storage_public_key_bytes", None)
        node_b_peer_key = getattr(node_a, "storage_public_key_bytes", None)
        self.assertIsNotNone(node_a_peer_key)
        self.assertIsNotNone(node_b_peer_key)
        print(f"node_a storage_public_key={node_a_peer_key.hex()}")
        print(f"node_b storage_public_key={node_b_peer_key.hex()}")

        deadline = time.time() + 10

        node_a_has_peer = False
        while time.time() < deadline:
            node_a_has_peer = get_peer(node_a, node_a_peer_key) is not None
            if node_a_has_peer:
                print("node_a sees node_b")
                break
            time.sleep(0.1)

        node_b_has_peer = False
        while time.time() < deadline:
            node_b_has_peer = get_peer(node_b, node_b_peer_key) is not None
            if node_b_has_peer:
                print("node_b sees node_a")
                break
            time.sleep(0.1)

        self.assertTrue(node_a_has_peer, "node_a did not register node_b before timeout")
        self.assertTrue(node_b_has_peer, "node_b did not register node_a before timeout")


if __name__ == "__main__":
    unittest.main()
