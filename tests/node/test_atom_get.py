from __future__ import annotations

import contextlib
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node  # noqa: E402
from astreum.storage.models.atom import Atom, AtomKind  # noqa: E402
from astreum.communication.util import xor_distance  # noqa: E402


class TestAtomGet(unittest.TestCase):
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
        for attr in ("incoming_socket", "outgoing_socket"):
            sock = getattr(node, attr, None)
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_storage_get_fetches_remote_atom(self) -> None:
        node_a_port = self._get_free_port()
        node_a = self._register_node(Node({"incoming_port": node_a_port, "verbose": True}))

        node_a_thread = threading.Thread(target=node_a.connect, daemon=True)
        node_a_thread.start()
        node_a_thread.join(timeout=5)
        self.assertTrue(node_a.is_connected)
        print(f"node_a connected incoming_port={node_a.incoming_port}")

        bootstrap_host = "127.0.0.1"
        bootstrap_port = node_a.incoming_port
        node_b_port = self._get_free_port()

        node_b = self._register_node(
            Node(
                {
                    "incoming_port": node_b_port,
                    "bootstrap": [f"{bootstrap_host}:{bootstrap_port}"],
                    "verbose": True,
                }
            )
        )

        node_b_thread = threading.Thread(target=node_b.connect, daemon=True)
        node_b_thread.start()
        node_b_thread.join(timeout=5)

        self.assertTrue(node_b.is_connected)
        print(
            f"node_b connected incoming_port={node_b.incoming_port} "
            f"bootstrap={bootstrap_host}:{bootstrap_port}"
        )

        node_a_peer_key = getattr(node_b, "relay_public_key_bytes", None)
        node_b_peer_key = getattr(node_a, "relay_public_key_bytes", None)
        self.assertIsNotNone(node_a_peer_key)
        self.assertIsNotNone(node_b_peer_key)
        print(f"node_a relay_public_key={node_a_peer_key.hex()}")
        print(f"node_b relay_public_key={node_b_peer_key.hex()}")
        atom = Atom(data=b"remote-atom", kind=AtomKind.BYTES)
        atom_id = atom.object_id()
        dist_a = xor_distance(atom_id, node_a_peer_key)
        dist_b = xor_distance(atom_id, node_b_peer_key)
        print(f"distance atom-node_a={dist_a} atom-node_b={dist_b}")

        deadline = time.time() + 10

        while time.time() < deadline:
            if node_a.get_peer(node_a_peer_key):
                print("node_a sees node_b")
                break
            time.sleep(0.1)
        else:
            self.fail("node_a did not register node_b before timeout")

        while time.time() < deadline:
            if node_b.get_peer(node_b_peer_key):
                print("node_b sees node_a")
                break
            time.sleep(0.1)
        else:
            self.fail("node_b did not register node_a before timeout")

        stored = node_a._hot_storage_set(key=atom_id, value=atom)
        self.assertTrue(stored, "node_a failed to store atom in hot storage")
        print(f"Stored atom on node_a id={atom_id.hex()} size={atom.size} data={atom.data!r}")
        node_a._network_set(atom_id)
        time.sleep(3)
        print(f"node_a storage_index keys={[k.hex() for k in node_a.storage_index.keys()]}")
        print(f"node_b storage_index keys={[k.hex() for k in node_b.storage_index.keys()]}")

        fetched_atom = None
        fetch_deadline = time.time() + 10
        print(f"Waiting for node_b.storage_get({atom_id.hex()}) to retrieve from network")
        while time.time() < fetch_deadline:
            fetched_atom = node_b.storage_get(atom_id)
            if fetched_atom is not None:
                print("node_b storage_get returned atom from network")
                break
            if not node_b.has_atom_req(atom_id):
                print("node_b.storage_get did not enqueue atom request")
            time.sleep(0.1)

        if fetched_atom is None:
            pending_request = node_b.has_atom_req(atom_id)
            print(
                f"node_b did not fetch atom {atom_id.hex()} "
                f"pending_request={pending_request} "
                f"hot_storage_keys={list(node_b.hot_storage.keys())}"
            )
        self.assertIsNotNone(fetched_atom, "node_b did not fetch atom from node_a before timeout")
        self.assertEqual(fetched_atom.object_id(), atom_id)
        self.assertEqual(fetched_atom.data, atom.data)


if __name__ == "__main__":
    unittest.main()
