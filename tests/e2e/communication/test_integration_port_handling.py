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

from astreum.node import Node
from astreum.communication.node import connect_node
from astreum.communication.models.peer import get_peer
from astreum.communication.models.message import Message, MessageTopic


class TestIntegrationPortHandling(unittest.TestCase):
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
        node.communication_stop_event.set()
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

    def test_peer_address_updated_uses_source_port(self) -> None:
        """
        Verify that when a message arrives from a different UDP source port,
        the peer's address is updated to reflect the new source port.
        """
        a_port = self._get_free_port()
        node_a = self._register_node(
            Node({"port": a_port, "default_seed": None, "logging_enabled": False})
        )
        connect_node(node_a)
        self.assertTrue(node_a.is_connected)

        b_port = self._get_free_port()
        node_b = self._register_node(
            Node(
                {
                    "port": b_port,
                    "default_seed": None,
                    "additional_seeds": [f"127.0.0.1:{a_port}"],
                    "logging_enabled": False,
                }
            )
        )
        connect_node(node_b)
        self.assertTrue(node_b.is_connected)

        deadline = time.time() + 10
        peer = None
        while time.time() < deadline:
            peer = get_peer(node_b, node_a.storage_public_key_bytes)
            if peer is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(peer, "Handshake did not complete in time")

        original_addr = peer.address
        self.assertIsNotNone(original_addr)

        msg = Message(
            topic=MessageTopic.PING,
            content=b"ping",
            sender_public_key_bytes=node_a.storage_public_key_bytes,
        )
        msg.encrypt(peer.shared_key_bytes)
        msg_bytes = msg.to_bytes()

        ephemeral_port = 54321
        node_b.incoming_queue.put(
            (msg_bytes, ("127.0.0.1", ephemeral_port), len(msg_bytes))
        )

        time.sleep(0.5)

        self.assertEqual(
            peer.address,
            ("127.0.0.1", ephemeral_port),
        )


if __name__ == "__main__":
    unittest.main()
