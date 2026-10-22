import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from astreum.consensus.validation.genesis import create_genesis_block
from astreum.consensus.validation.validator import current_validator
from astreum.consensus.constants import TREASURY_ADDRESS
from astreum.expression import resolve_inner_exprs
from astreum.consensus.models.accounts import extract_accounts_exprs
from astreum.storage.exprs import put_expr_in_hot_storage
from astreum.node import Node


class TestNodeValidator(unittest.TestCase):
    def test_current_validator_after_genesis(self) -> None:
        node = Node()
        private_key = Ed25519PrivateKey.generate()
        validator_public_key = private_key.public_key().public_bytes(
            Encoding.Raw,
            PublicFormat.Raw,
        )
        block = create_genesis_block(node, validator_public_key)
        self.assertIsNotNone(block.accounts, "genesis block must expose the accounts trie")

        block.accounts.update_trie(node)

        from astreum.consensus.block.nonce import generate_block_nonce
        generate_block_nonce(block=block, difficulty=1)
        from astreum.consensus.block.encoding.expr import get_block_expr
        block_hash = get_block_expr(block).hash()
        block_exprs, _ = resolve_inner_exprs(node, get_block_expr(block))

        for e in extract_accounts_exprs(block.accounts):
            put_expr_in_hot_storage(node, e)
        for e in block_exprs:
            put_expr_in_hot_storage(node, e)

        validator_key, _ = current_validator(
            node=node,
            block_hash=block_hash,
            target_time=block.timestamp + 1,
        )

        self.assertEqual(validator_key, validator_public_key)


if __name__ == "__main__":
    unittest.main()
