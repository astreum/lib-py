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

from astreum.communication.models.relay_endpoint import RelayEndpoint  # noqa: E402
from astreum.node import Node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.expression import Expr, ZERO32, resolve_inner_exprs  # noqa: E402
from astreum.consensus.constants import TREASURY_ADDRESS  # noqa: E402
from astreum.consensus.models.block import Block  # noqa: E402
from astreum.consensus.block.encoding.expr import get_block_expr  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.communication.difficulty import message_difficulty  # noqa: E402
from astreum.communication.models.peer import get_peer  # noqa: E402
from astreum.communication.node import connect_node
from astreum.storage.exprs import get_expr_list  # noqa: E402
from astreum.communication.disconnect import disconnect_node


class TestNodeValidation(unittest.TestCase):
    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_latest_block_stored_locally(self) -> None:
        port = self._get_free_port()
        node = Node(
            config={
                "port": port,
                "default_seed": None,
                "additional_seeds": [],
                "storage_index_interval": 1,
                "verbose": True,
                "atom_fetch_interval": 1,
                "verbose": True,
            }
        )
        try:
            secret_key = Ed25519PrivateKey.generate()
            validate_blockchain(node, secret_key)

            time.sleep(5)
            latest_hash = node.latest_block_hash
            self.assertIsNotNone(latest_hash)

            loaded = get_block_from_storage(astreum_node=node, block_hash=latest_hash)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.expr_id, latest_hash)
        finally:
            disconnect_node(node)

    def test_validate_initializes_genesis_block(self) -> None:
        node = Node()
        connect_node(node)

        secret_key = Ed25519PrivateKey.generate()
        validate_blockchain(node, secret_key)

        b = node.latest_block
        self.assertIsNotNone(node.latest_block_hash)
        self.assertIsNotNone(b)
        self.assertEqual(b.height, 0)
        self.assertEqual(b.previous_block_hash, ZERO32)
        self.assertEqual(b.chain_id, 0)
        self.assertEqual(node.latest_block_hash, get_block_expr(b).hash())

        vk = node.config.validation_public_key_bytes
        validator_account = b.accounts.get_account(vk, node)
        self.assertIsNotNone(validator_account, "validator account should exist in genesis")
        treasury_account = b.accounts.get_account(TREASURY_ADDRESS, node)
        self.assertIsNotNone(treasury_account, "treasury account should exist in genesis")
        

    def test_latest_block_loads_from_default_seed(self) -> None:
        port_a = self._get_free_port()
        node_a = Node(
            config={
                "port": port_a,
                "default_seed": None,
                "additional_seeds": [],
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "verbose": False,
                "logger_name": "node a",
            }
        )
        connect_node(node_a)
        node_b = None
        try:
            secret_key = Ed25519PrivateKey.generate()
            validate_blockchain(node_a, secret_key)

            timeout = time.time() + 10
            while time.time() < timeout:
                latest_a = node_a.latest_block
                if latest_a is not None and int(latest_a.height or 0) >= 1:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(node_a.latest_block)

            port_a = node_a.config.port
            port_b = self._get_free_port()
            node_b = Node(
                config={
                    "port": port_b,
                    "default_seed": f"127.0.0.1:{port_a}",
                    "additional_seeds": [],
                    "storage_index_interval": 1,
                    "atom_fetch_interval": 8,
                    "verbose": False,
                    "logger_name": "node b",
                }
            )
            connect_node(node_b)

            # Peers are keyed by storage public key, not relay public key.
            node_a_peer_key = getattr(node_b, "storage_public_key_bytes", None)
            node_b_peer_key = getattr(node_a, "storage_public_key_bytes", None)
            self.assertIsNotNone(node_a_peer_key)
            self.assertIsNotNone(node_b_peer_key)

            deadline = time.time() + 10
            while time.time() < deadline:
                if get_peer(node_a, node_a_peer_key):
                    break
                time.sleep(0.1)
            else:
                self.fail("node_a did not register node_b before timeout")

            deadline = time.time() + 10
            while time.time() < deadline:
                if get_peer(node_b, node_b_peer_key):
                    break
                time.sleep(0.1)
            else:
                self.fail("node_b did not register node_a before timeout")

            time.sleep(5)
            timeout = time.time() + 5
            while time.time() < timeout:
                if node_b.latest_block_hash is not None:
                    break
                time.sleep(0.1)

            latest_hash = node_b.latest_block_hash
            self.assertIsNotNone(
                latest_hash,
                "node B should receive latest_block_hash after connecting to node A",
            )
            # storage_index is filled only from on-chain storage records, so a
            # new block hash is not expected to be in it.

            node_a_block = get_block_from_storage(astreum_node=node_a, block_hash=latest_hash)
            self.assertIsNotNone(
                node_a_block,
                "node A should load latest block from storage",
            )

            # A new block's exprs exist only on node A, and node B is not a
            # validator, so it gets no BLOCK announcement. Give B the contact.
            contact_a = RelayEndpoint(node_a.relay_public_key_bytes, ("127.0.0.1", port_a))

            node_a_header = get_expr_list(node_a, latest_hash)
            self.assertIsNotNone(
                node_a_header,
                "node A should load latest block header from storage",
            )
            self.assertIsNotNone(
                node_a_block.body_hash,
                "node A should have a block body list hash",
            )
            body_expr = get_expr_list(node_a, node_a_block.body_hash)
            self.assertIsNotNone(
                body_expr,
                "node A should load block body list from storage",
            )

            loaded_block = None
            for _ in range(3):
                header_b = get_expr_list(node_b, latest_hash, contact=contact_a, block_hash=latest_hash)
                self.assertIsNotNone(
                    header_b,
                    "node B should load latest block header from storage",
                )
                # An expr hash is a content hash, so equal hashes mean equal trees.
                self.assertEqual(
                    node_a_header.hash(),
                    header_b.hash(),
                    "node B header list should match node A",
                )
                if node_a_block.body_hash is not None:
                    body_b = get_expr_list(
                        node_b,
                        node_a_block.body_hash,
                        contact=contact_a,
                        block_hash=latest_hash,
                    )
                    self.assertIsNotNone(
                        body_b,
                        "node B should load block body list from storage",
                    )
                    self.assertEqual(
                        body_expr.hash(),
                        body_b.hash(),
                        "node B body list should match node A",
                    )
                loaded_block = get_block_from_storage(astreum_node=node_b, block_hash=latest_hash, contact=contact_a)
                if loaded_block is not None:
                    break
                time.sleep(3)

            self.assertIsNotNone(
                loaded_block,
                "node B should load latest block from storage",
            )
        finally:
            if node_b is not None:
                disconnect_node(node_b)
            disconnect_node(node_a)


if __name__ == "__main__":

    unittest.main()
