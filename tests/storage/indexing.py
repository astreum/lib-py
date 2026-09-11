from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.expression import (
    RESOLUTION_LIST,
    RESOLUTION_SINGLE,
    ZERO32,
    Expr,
    resolve_inner_exprs,
    resolve_list_exprs,
)
from astreum.consensus.account import create_account
from astreum.consensus.block.create import create_block
from astreum.consensus.constants import STORAGE_ADDRESS
from astreum.consensus.transaction.storage.model import StorageRecord
from astreum.consensus.models.accounts import Accounts
from astreum.node import Node
from astreum.communication.node import connect_node
from astreum.communication.disconnect import disconnect_node
from astreum.communication.models.peer import get_peer
from astreum.storage.exprs import get_expr
from astreum.storage.exprs import get_expr_list
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.storage.advertisements import advertise_exprs
from astreum.storage.radix import put_in_radix_tree
from tests.storage.utils import generate_nearest_expr, generate_nearest_expr_list


class TestStorageIndexing(unittest.TestCase):
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
        disconnect_node(node)

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def _connect_nodes(self) -> tuple[Node, Node]:
        node_a_port = self._get_free_port()
        node_a = self._register_node(
            Node(
                {
                    "port": node_a_port,
                    "default_seed": None,
                    "verbose": True,
                    "storage_secret_key": Ed25519PrivateKey.generate(),
                }
            )
        )

        node_a_thread = threading.Thread(
            target=connect_node, args=(node_a,), daemon=True
        )
        node_a_thread.start()
        node_a_thread.join(timeout=5)
        self.assertTrue(node_a.is_connected)

        bootstrap_host = "127.0.0.1"
        bootstrap_port = node_a.config["port"]
        node_b_port = self._get_free_port()

        node_b = self._register_node(
            Node(
                {
                    "port": node_b_port,
                    "default_seed": None,
                    "additional_seeds": [f"{bootstrap_host}:{bootstrap_port}"],
                    "verbose": True,
                    "storage_secret_key": Ed25519PrivateKey.generate(),
                }
            )
        )

        node_b_thread = threading.Thread(
            target=connect_node, args=(node_b,), daemon=True
        )
        node_b_thread.start()
        node_b_thread.join(timeout=5)

        self.assertTrue(node_b.is_connected)

        node_a_peer_key = node_b.storage_public_key_bytes
        node_b_peer_key = node_a.storage_public_key_bytes
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

        return node_a, node_b

    @staticmethod
    def _commit_storage_keys(node: Node, *keys: bytes) -> None:
        """Commit *keys* into *node*'s latest block storage-account trie.

        The ``STORAGE_PUT`` admission gate requires the advertised expr to be
        a key in the latest block's storage-account data trie, and the index
        gate requires its value to parse as a record header.  Build a real
        (in-memory) ``Block`` whose ``STORAGE_ADDRESS`` account holds a
        ``RadixTree``, then insert a ``StorageRecord`` header for each key.
        """
        tree = getattr(node, "_test_storage_tree", None)
        if tree is None:
            block = create_block(
                chain_id=node.config["chain_id"],
                previous_block_hash=ZERO32,
                previous_block=None,
                height=0,
                timestamp=0,
                accounts_hash=ZERO32,
                total_transaction_fee=0,
                total_storage_fee=0,
                transactions_hash=ZERO32,
                receipts_hash=ZERO32,
                difficulty=1,
                validator_public_key_bytes=os.urandom(32),
                expr_id=os.urandom(32),
                accounts=Accounts(),
            )
            storage_account = create_account()
            block.accounts.set_account(STORAGE_ADDRESS, storage_account)
            tree = storage_account.data
            node._test_storage_tree = tree
            node.latest_block = block
            node.latest_block_hash = block.expr_id
        record_expr = StorageRecord(
            creation_block_hash=ZERO32,
            last_payment_block_hash=ZERO32,
            last_payment_height=0,
            last_payment_winner=ZERO32,
            new_size=0,
            new_count=0,
            mint=True,
        ).expr()
        for key in keys:
            put_in_radix_tree(tree, node, key, record_expr)

    def test_closest_atom_advertisement(self) -> None:
        """
        Test that an advertisement for an expr closer to node_b is indexed by node_b.
        1. Connect node_a and node_b.
        2. Create an expr closest to node_b.
        3. Advertise it immediately from node_a.
        4. Wait for the object to be seen in node_b index.
        5. Fetch the expr and list from node_b.
        """
        node_a, node_b = self._connect_nodes()

        print(f"Node A ID: {node_a.storage_public_key_bytes.hex()}")
        print(f"Node B ID: {node_b.storage_public_key_bytes.hex()}")

        def wait_for_index(expr_id: bytes, label: str) -> None:
            deadline = time.time() + 10
            print(f"Waiting for {label} to appear in Node B's index...")
            while time.time() < deadline:
                provider_id = node_b.storage_index.get(expr_id)
                if provider_id is not None:
                    print(f"{label} found in index! Provider ID: {provider_id}")
                    return
                time.sleep(0.1)
            self.fail(f"Node B did not index the advertised {label}")

        def wait_for_expr(expr_id: bytes, label: str) -> None:
            deadline = time.time() + 10
            print(f"Waiting for {label} to be fetched by Node B...")
            while time.time() < deadline:
                expr = get_expr(node_b, expr_id)
                if expr is not None:
                    print(f"{label} fetched by Node B.")
                    return
                time.sleep(0.1)
            self.fail(f"Node B did not fetch the advertised {label}")

        def wait_for_list(root_id: bytes, label: str, expected_size: int) -> None:
            deadline = time.time() + 10
            print(f"Waiting for {label} to be fetched by Node B...")
            while time.time() < deadline:
                header = get_expr_list(node_b, root_id)
                if header is not None:
                    items, _ = resolve_list_exprs(node_b, header)
                    self.assertEqual(
                        len(items),
                        expected_size,
                        "node_b returned an unexpected list size",
                    )
                    print(f"{label} fetched by Node B.")
                    return
                time.sleep(0.1)
            self.fail(f"Node B did not fetch the advertised {label}")

        target_expr = generate_nearest_expr(
            node_a.storage_public_key_bytes,
            node_b.storage_public_key_bytes,
        )
        expr_id = target_expr.hash()

        # Store in A so it can serve/advertise it
        exprs, _ = resolve_inner_exprs(node_a, target_expr)
        for expr in exprs:
            self.assertTrue(put_expr_in_hot_storage(node_a, expr), "node_a failed to store expr")

        # Commit the expr and its sub-exprs into node_b's latest block before
        # advertising: the admission gate rejects uncommitted exprs on both the
        # incoming STORAGE_PUT and the fetched STORAGE_FOUND response.
        self._commit_storage_keys(node_b, *(e.hash() for e in exprs))

        # Advertise it immediately
        print("Advertising expr from Node A...")
        advertise_exprs(node_a, entries=[(expr_id, RESOLUTION_SINGLE, None)])
        wait_for_index(expr_id, "expr")
        wait_for_expr(expr_id, "expr")

        list_size = 4
        list_chain = generate_nearest_expr_list(
            node_a.storage_public_key_bytes,
            node_b.storage_public_key_bytes,
            list_size=list_size,
        )
        list_root_id = list_chain.hash()
        list_exprs, _ = resolve_inner_exprs(node_a, list_chain)
        for expr in list_exprs:
            self.assertTrue(put_expr_in_hot_storage(node_a, expr), "node_a failed to store list expr")
        self._commit_storage_keys(node_b, *(e.hash() for e in list_exprs))

        print("Advertising list from Node A...")
        advertise_exprs(node_a, entries=[(list_root_id, RESOLUTION_LIST, None)])
        wait_for_index(list_root_id, "list")
        wait_for_list(list_root_id, "list", list_size)


if __name__ == "__main__":
    unittest.main()
