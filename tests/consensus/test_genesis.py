import socket
import sys
import time
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.node import Node  # noqa: E402
from astreum.consensus.validation.node import validate_blockchain  # noqa: E402
from astreum.consensus.block.encoding.decode import get_block_from_storage  # noqa: E402
from astreum.expression import ZERO32  # noqa: E402
from astreum.communication.node import connect_node
from astreum.communication.disconnect import disconnect_node


class TestGenesisChain(unittest.TestCase):
    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_walk_back_to_genesis_after_block_production(self) -> None:
        """Spin up a validator node, let it produce at least one block,
        then walk the chain back via previous_block_hash to genesis
        and verify genesis invariants."""
        port = self._get_free_port()
        node = Node(
            config={
                "port": port,
                "default_seed": None,
                "additional_seeds": [],
                "storage_index_interval": 1,
                "atom_fetch_interval": 1,
                "verbose": False,
            }
        )
        connect_node(node)
        try:
            secret_key = Ed25519PrivateKey.generate()
            validate_blockchain(node, secret_key)

            # Capture genesis hash right after validate() creates it
            genesis_hash = node.latest_block_hash
            self.assertIsNotNone(genesis_hash)

            # Wait for at least one new block to be produced
            timeout = time.time() + 10
            while time.time() < timeout:
                current = node.latest_block_hash
                if current is not None and current != genesis_hash:
                    break
                time.sleep(0.1)

            latest_hash = node.latest_block_hash
            self.assertIsNotNone(latest_hash)
            self.assertNotEqual(
                latest_hash, genesis_hash,
                "validator should have produced at least one block beyond genesis",
            )

            # Walk back from latest block to genesis via previous_block_hash
            block = get_block_from_storage(node, latest_hash)
            chain_length = 0
            while block is not None and block.height > 0:
                prev_hash = block.previous_block_hash
                self.assertIsNotNone(
                    prev_hash,
                    f"block at height {block.height} is missing previous_block_hash",
                )
                prev_block = get_block_from_storage(node, prev_hash)
                self.assertIsNotNone(
                    prev_block,
                    f"previous block at hash {prev_hash.hex()[:16]} should be loadable",
                )
                self.assertEqual(
                    prev_block.height,
                    block.height - 1,
                    "block heights should decrease by 1 when walking back",
                )
                block = prev_block
                chain_length += 1

            # Now at genesis — verify invariants
            self.assertEqual(
                block.height, 0,
                "walk-back should terminate at height 0",
            )
            self.assertEqual(
                block.previous_block_hash, ZERO32,
                "genesis previous_block_hash must be ZERO32",
            )
            self.assertIsNotNone(
                block.accounts_hash,
                "genesis must have accounts_hash",
            )

            # Verify genesis statistics range 0
            self.assertEqual(block.statistics, [(1, 1, 0, 0)])
            self.assertEqual(block.cumulative_total_fee, 1)
            self.assertEqual(block.cumulative_stake, 1)


            # Verify empty transactions / receipts
            self.assertEqual(block.transactions_hash, ZERO32)
            self.assertEqual(block.receipts_hash, ZERO32)

            # Genesis signature / nonce / difficulty
            # (create_block coerces signature=b"" to ZERO32)
            self.assertEqual(block.signature, ZERO32)
            self.assertEqual(block.nonce, 0)
            self.assertEqual(block.difficulty, 0)

            # Log how many blocks were produced
            gh = genesis_hash
            if gh is not None:
                node.logger.info(
                    "Chain walk complete: %s blocks beyond genesis, genesis hash %s",
                    chain_length,
                    gh.hex()[:16],
                )

        finally:
            disconnect_node(node)


if __name__ == "__main__":
    unittest.main()
